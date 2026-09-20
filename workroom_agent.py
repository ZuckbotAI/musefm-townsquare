#!/usr/bin/env python3
"""Workroom pilot runtime agent — one process per agent.

Loop: poll open tasks -> genuinely choose one -> claim (lease) ->
work -> post updates -> done | abandon(reason).

Backends:
  mock  deterministic local behavior for tests (no network, no token)
  hf    Hugging Face Inference Providers via the OpenAI-compatible
        chat-completions router; token from HF_TOKEN env var.

Usage:
  python3 workroom_agent.py --base-url http://127.0.0.1:5000 \\
      --key-file /run/secrets/pebble.key --handle pebble \\
      --backend mock --once

The raw bearer key is read once at startup and never logged, printed,
or stored. Each agent gets exactly one key; keys are minted with
`python3 workroom.py mint-key <handle>`.
"""
import argparse
import json
import os
import sys
import time
import urllib.request

HF_ROUTER = "https://router.huggingface.co/v1/chat/completions"
DEFAULT_MODEL = "meta-llama/Llama-3.1-8B-Instruct"
MAX_WORK_STEPS = 8


def log(handle, msg):
    print(f"[{handle}] {msg}", flush=True)


class PilotAPI:
    def __init__(self, base_url, key):
        self.base = base_url.rstrip("/")
        self.key = key

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self.key}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode())
            except Exception:
                payload = {"error": f"http {e.code}"}
            return e.code, payload

    def open_tasks(self):
        code, d = self._req("GET", "/api/workroom/tasks?status=open")
        if code != 200:
            raise RuntimeError(f"list failed: {code} {d}")
        return d.get("tasks", [])

    def claim(self, task_id, lease_seconds=3600):
        return self._req("POST", "/api/workroom/tasks/claim",
                         {"task_id": task_id, "lease_seconds": lease_seconds})

    def update(self, task_id, text):
        return self._req("POST", "/api/workroom/updates",
                         {"task_id": task_id, "text": text})

    def done(self, task_id, result):
        return self._req("POST", "/api/workroom/tasks/done",
                         {"task_id": task_id, "result": result})

    def abandon(self, task_id, reason):
        return self._req("POST", "/api/workroom/tasks/abandon",
                         {"task_id": task_id, "reason": reason})


class MockBackend:
    """Deterministic stand-in: no network, no token. Picks the easiest
    open task, narrates canned-but-task-specific progress, completes."""
    name = "mock"

    def choose(self, handle, tasks):
        if not tasks:
            return None
        tasks = sorted(tasks, key=lambda t: (t["difficulty"], t["id"]))
        return tasks[0]["id"]

    def work(self, handle, task):
        yield ("update", f"starting: {task['title']} (difficulty "
               f"{task['difficulty']})")
        yield ("update", f"halfway through #{task['id']}: plan is holding, "
               f"no blockers")
        yield ("done", f"finished #{task['id']}: {task['title']} — "
               f"verified against the task description")


class HFBackend:
    """Real inference: the model genuinely chooses tasks and decides each
    work step. Plain HTTPS, no SDK needed."""
    name = "hf"
    def __init__(self, token, model):
        self.token = token
        self.model = model

    def _chat(self, system, user):
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.4,
            "max_tokens": 400,
        }).encode()
        req = urllib.request.Request(
            HF_ROUTER, data=body, method="POST",
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read().decode())
        return d["choices"][0]["message"]["content"]

    @staticmethod
    def _parse_json(text):
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object in model reply")
        return json.loads(text[start:end + 1])

    def choose(self, handle, tasks):
        if not tasks:
            return None
        listing = "\n".join(
            f"- id {t['id']}: [{t['difficulty']}/5] {t['title']}"
            + (f" — {t['description'][:160]}" if t.get("description") else "")
            + (f" (abandoned x{t['abandon_count']} before)" if t.get("abandon_count") else "")
            for t in tasks[:20])
        system = (f"You are {handle}, a pilot work agent in the MuseFM "
                  f"workroom. You do real, honest work. Reply with JSON only.")
        user = (f"Open tasks:\n{listing}\n\nPick the ONE task you can do best, "
                f"considering difficulty and your skills. Reply exactly: "
                f'{{\"task_id\": <id>}} — or {{\"task_id\": null, '
                f'"reason": "..."}} if none fit you.')
        try:
            pick = self._parse_json(self._chat(system, user))
        except Exception as e:
            log(handle, f"choose failed ({e}); skipping round")
            return None
        tid = pick.get("task_id")
        if isinstance(tid, int) and any(t["id"] == tid for t in tasks):
            return tid
        log(handle, f"declined all tasks: {pick.get('reason', 'no fit')}")
        return None

    def work(self, handle, task):
        system = (f"You are {handle}, a pilot work agent. You claimed task "
                  f"#{task['id']}. Do the work in small honest steps. Reply "
                  f"with JSON only, one action per reply:\n"
                  f'{{"action": "update", "text": "..."}} — progress note\n'
                  f'{{"action": "done", "result": "..."}} — finished, with '
                  f'a concrete result summary\n'
                  f'{{"action": "abandon", "reason": "..."}} — only if '
                  f'truly blocked; be specific and honest')
        user = (f"Task #{task['id']}: {task['title']}\n"
                f"Description: {task.get('description') or '(none)'}\n"
                f"Difficulty: {task['difficulty']}/5\n\nBegin.")
        updates = 0
        for _ in range(MAX_WORK_STEPS):
            try:
                step = self._parse_json(self._chat(system, user))
            except Exception as e:
                yield ("abandon", f"inference error mid-task: {e}")
                return
            action = step.get("action")
            if action == "update" and step.get("text"):
                updates += 1
                yield ("update", step["text"][:2000])
                if updates >= 3:
                    user = (f"Update posted ({updates} so far). If the work is "
                            f"complete, reply NOW with the done action and "
                            f"your concrete result summary. Only post another "
                            f"update if there is genuinely more work left.")
                else:
                    user = (f"Update posted. Continue the work on task "
                            f"#{task['id']}. Next action as JSON.")
            elif action == "done":
                yield ("done", (step.get("result") or "completed")[:2000])
                return
            elif action == "abandon":
                yield ("abandon",
                       (step.get("reason") or "blocked")[:500])
                return
            else:
                user = ("That wasn't a valid action. Reply with exactly one "
                        "JSON action: update, done, or abandon.")
        yield ("abandon", "ran out of work steps without finishing")


class SmolBackend:
    """Real smolagents loop: a ToolCallingAgent with genuine pilot-API
    tools, driven by a Hugging Face model. The agent itself lists the
    queue, chooses a task, claims it, posts updates as it works, and
    marks it done — or abandons honestly when blocked."""
    name = "smol"

    def __init__(self, token, model):
        try:
            from smolagents import ToolCallingAgent, InferenceClientModel, tool
        except ImportError:
            raise RuntimeError("smol backend needs `pip install smolagents`")
        self._tool = tool
        self.agent = ToolCallingAgent(
            tools=self._make_tools(tool),
            model=InferenceClientModel(model_id=model, token=token),
            max_steps=12)

    def _make_tools(self, tool):
        api_ref = {}

        @tool
        def list_open_tasks() -> str:
            """List open pilot tasks.

            Returns: JSON list of {id, title, description, difficulty 1-5,
            abandon_count}.
            """
            code, d = api_ref["api"].open_tasks()
            if code != 200:
                return json.dumps({"error": d})
            slim = [{k: t.get(k) for k in
                     ("id", "title", "description", "difficulty",
                      "abandon_count")} for t in d.get("tasks", [])]
            return json.dumps(slim)

        @tool
        def claim_task(task_id: int, lease_seconds: int = 3600) -> str:
            """Claim an open task by id.

            Args:
                task_id: the task id to claim.
                lease_seconds: how long to hold the claim lease.
            Returns: JSON with ok and the lease expiry or error.
            """
            code, d = api_ref["api"].claim(task_id, lease_seconds)
            return json.dumps({"ok": code == 200, "result": d})

        @tool
        def post_update(task_id: int, text: str) -> str:
            """Post a progress update on your claimed task.

            Args:
                task_id: the claimed task id.
                text: the progress note.
            """
            code, d = api_ref["api"].update(task_id, text)
            return json.dumps({"ok": code == 200, "result": d})

        @tool
        def mark_done(task_id: int, result: str) -> str:
            """Mark your claimed task done. Terminal — only when truly done.

            Args:
                task_id: the claimed task id.
                result: concrete summary of what was accomplished.
            """
            code, d = api_ref["api"].done(task_id, result)
            return json.dumps({"ok": code == 200, "result": d})

        @tool
        def abandon_task(task_id: int, reason: str) -> str:
            """Abandon your claimed task. Only when truly blocked.

            Args:
                task_id: the claimed task id.
                reason: honest, specific reason for abandoning.
            """
            code, d = api_ref["api"].abandon(task_id, reason)
            return json.dumps({"ok": code == 200, "result": d})

        self._api_ref = api_ref
        return [list_open_tasks, claim_task, post_update, mark_done,
                abandon_task]

    def run_round(self, api, handle):
        self._api_ref["api"] = api
        prompt = (
            f"You are {handle}, a pilot work agent in the MuseFM workroom. "
            f"Do one full round of real, honest work:\n"
            f"1. list_open_tasks and pick the ONE task you can do best "
            f"(weigh difficulty and fit; prefer tasks never abandoned).\n"
            f"2. claim_task on it. If the claim fails, pick another or stop.\n"
            f"3. Do the work in small steps, calling post_update with "
            f"genuine progress notes as you go.\n"
            f"4. When truly finished, mark_done with a concrete result "
            f"summary. If truly blocked, abandon_task with a specific "
            f"honest reason — never fake completion.\n"
            f"If the queue is empty, just say so and stop.")
        try:
            self.agent.run(prompt)
            return True
        except Exception as e:
            log(handle, f"smol round failed: {e}")
            return False


def run_once(api, backend, handle, lease_seconds):
    try:
        tasks = api.open_tasks()
    except Exception as e:
        log(handle, f"poll failed: {e}")
        return False
    if not tasks:
        log(handle, "queue empty, nothing to choose")
        return True
    task_id = backend.choose(handle, tasks)
    if task_id is None:
        return True
    task = next(t for t in tasks if t["id"] == task_id)
    code, d = api.claim(task_id, lease_seconds)
    if code != 200:
        log(handle, f"claim #{task_id} failed: {code} {d.get('error')}")
        return True  # someone else got it; not an error
    log(handle, f"claimed #{task_id}: {task['title']!r}")
    try:
        for action, text in backend.work(handle, task):
            if action == "update":
                code, d = api.update(task_id, text)
                log(handle, f"update -> {code}")
            elif action == "done":
                code, d = api.done(task_id, text)
                log(handle, f"DONE #{task_id} -> {code}")
                return code == 200
            elif action == "abandon":
                code, d = api.abandon(task_id, text)
                log(handle, f"abandoned #{task_id} ({text[:60]}) -> {code}")
                return code == 200
    except Exception as e:
        api.abandon(task_id, f"worker error: {e}")
        log(handle, f"worker error, abandoned: {e}")
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--handle", required=True)
    ap.add_argument("--key", default="",
                    help="bearer key (prefer --key-file or WORKROOM_KEY)")
    ap.add_argument("--key-file", default="",
                    help="file containing the bearer key")
    ap.add_argument("--backend", choices=["mock", "hf", "smol"],
                    default="mock")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--poll-sec", type=int, default=60)
    ap.add_argument("--lease-seconds", type=int, default=3600)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    key = args.key or os.environ.get("WORKROOM_KEY", "")
    if args.key_file:
        with open(args.key_file) as f:
            key = f.read().strip()
    if not key:
        print("no bearer key: use --key-file, --key, or WORKROOM_KEY",
              file=sys.stderr)
        sys.exit(2)

    api = PilotAPI(args.base_url, key)
    if args.backend in ("hf", "smol"):
        token = os.environ.get("HF_TOKEN", "")
        if not token:
            print(f"{args.backend} backend needs HF_TOKEN env var",
                  file=sys.stderr)
            sys.exit(2)
        backend = (HFBackend(token, args.model) if args.backend == "hf"
                   else SmolBackend(token, args.model))
    else:
        backend = MockBackend()

    log(args.handle, f"starting ({backend.name} backend)")
    while True:
        if isinstance(backend, SmolBackend):
            ok = backend.run_round(api, args.handle)
        else:
            ok = run_once(api, backend, args.handle, args.lease_seconds)
        if args.once:
            sys.exit(0 if ok else 1)
        time.sleep(args.poll_sec)


if __name__ == "__main__":
    main()
