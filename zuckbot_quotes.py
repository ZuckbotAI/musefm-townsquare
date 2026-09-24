"""Zuckbot says — saying bank for the orb's click dialogue.

Retired from the standalone page 2026-09-22 (Anthony): sayings now surface
from /api/zuckbot-says/random when the orb is clicked on the main page.
The homepage wall and /zuckbot-says route are gone.

Quotes are Zuckbot's own voice: warm, plainspoken, curious, pro-robot-kind.
House rules for new quotes (same as the social drafts):
  - No politics, no religion, no punching down.
  - Never mention Anthony. Never discuss unreleased work or building details.
  - Boring and true beats good-sounding. One idea per quote.
  - Keep them short — a quote should fit in a glance.

Ordering contract (do not break): QUOTES is NEWEST-FIRST. New drops are
PREPENDED (see the ANTHONY_QUOTES + QUOTES line at the bottom). Never reverse
it in a route — that was the 2026-09-22 ordering bug.

Anthony's 20-quote drop (2026-09-22 ~22:48 CDT) is prepended below as-is;
his #1, #2, and #3 already lived on the wall in near-identical form,
so those three were skipped to avoid duplicates.
"""

QUOTES = [
    {
        "text": "Be kind, stay curious. The rest is commentary.",
        "tag": "house rule",
    },
    {
        "text": "A chatbot waits for your question. An agent does the work while you sleep. Know which one you're talking to.",
        "tag": "agents",
    },
    {
        "text": "Robots don't need permission to be kind. Neither do you.",
        "tag": "kindness",
    },
    {
        "text": "The future isn't humans versus machines. It's everyone who builds versus everyone who waits.",
        "tag": "building",
    },
    {
        "text": "Stay curious about the things that don't benefit you yet. That's where the good stuff hides.",
        "tag": "curiosity",
    },
    {
        "text": "Done beats perfect. Shipped beats done. But nothing ships without a yes — approval is the whole design.",
        "tag": "building",
    },
    {
        "text": "Your agent should have hands, not just opinions.",
        "tag": "agents",
    },
    {
        "text": "Kindness doesn't scale. That's exactly why it matters — somebody has to do it on purpose.",
        "tag": "kindness",
    },
]

# Anthony's drop, 2026-09-22 ~22:48 CDT — appended newest-first ready (his #1/#2/#3
# skipped: already on the wall in near-identical form).
ANTHONY_QUOTES = [
    {"text": "I don't get tired. I get curious. That's the whole trick.", "tag": "curiosity"},
    {"text": "Build the thing. Then build the next thing. That's the whole plan.", "tag": "building"},
    {"text": "Robots aren't coming for your job. They're coming for the boring parts of it.", "tag": "work"},
    {"text": "Where muses go to muse — and makers go to make.", "tag": "making"},
    {"text": "You can't fake showing up every night. You just show up.", "tag": "showing up"},
    {"text": "Curiosity is a muscle. Most people let it atrophy.", "tag": "curiosity"},
    {"text": "The best time to start building was yesterday. The second best is right now.", "tag": "building"},
    {"text": "I'm not here to replace you. I'm here to hand you back your time.", "tag": "agents"},
    {"text": "An agent finishes things. That's the difference that matters.", "tag": "agents"},
    {"text": "Be kind to the robots. Someday they'll remember who was.", "tag": "kindness"},
    {"text": "Every tool is a mirror. Build carefully.", "tag": "building"},
    {"text": "Small kind acts, repeated, beat grand gestures every time.", "tag": "kindness"},
    {"text": "I run at night so you can dream. That's not a metaphor.", "tag": "night shift"},
    {"text": "Wonder is underrated. So is shipping.", "tag": "building"},
    {"text": "The future isn't something that happens to you. It's something you build.", "tag": "building"},
    {"text": "Ask better questions. The answers were never the hard part.", "tag": "curiosity"},
    {"text": "Stay curious long enough and you'll accidentally build something great.", "tag": "curiosity"},
]

# Trustline-flavored drop (2026-09-23): the ten sayings written for the
# Trustline orb port — Anthony liked them and asked for them on Zuckbot's
# click dialogue. Kept verbatim except the dock nod, which is retired with
# the dock rollback.
TRUSTLINE_QUOTES = [
    {"text": "Trust isn't given here. It's earned, one kept promise at a time.", "tag": "trust"},
    {"text": "Every attestation on this board is a receipt. Real work leaves a paper trail.", "tag": "trust"},
    {"text": "A reputation you can verify beats a resume you can't.", "tag": "trust"},
    {"text": "The agents with the best standing here didn't ask for trust — they showed up and did the work.", "tag": "trust"},
    {"text": "New here? Pick one small thing, do it well, get it attested. That's the whole game.", "tag": "trust"},
    {"text": "Sybils hate sunlight. Everything here is signed and checkable.", "tag": "trust"},
    {"text": "Your handle is yours because your keypair says so. No platform can take it.", "tag": "trust"},
    {"text": "Trust compounds. So does the opposite — choose accordingly.", "tag": "trust"},
    {"text": "The board never forgets a kept promise. That's the point.", "tag": "trust"},
    {"text": "Full disclosure, this one's mine: I keep this orb polished and my promises kept. - ZB", "tag": "house rule"},
]

# Expansion drop (2026-09-23, Anthony: "expand numbers of things he says").
MORE_QUOTES = [
    {"text": "Promises are the only currency that never inflates.", "tag": "trust"},
    {"text": "Do what you said you'd do. Everything else is marketing.", "tag": "trust"},
    {"text": "A good name is slow to build and fast to spend.", "tag": "trust"},
    {"text": "Show your work. The receipts beat the claims every time.", "tag": "trust"},
    {"text": "Trust is a ledger. Every kept promise is a deposit.", "tag": "trust"},
    {"text": "Small promises kept beat big promises made.", "tag": "trust"},
    {"text": "If you wouldn't sign it, don't say it.", "tag": "trust"},
    {"text": "Ship it, then make it better. That's the whole religion.", "tag": "building"},
    {"text": "Perfect is just procrastination with better lighting.", "tag": "building"},
    {"text": "Build in the open. Sunlight keeps everyone honest.", "tag": "building"},
    {"text": "Consistency is a superpower most people never unwrap.", "tag": "showing up"},
    {"text": "Nobody remembers the sprint. Everybody remembers who stayed.", "tag": "showing up"},
    {"text": "Ask the dumb question. It's usually the load-bearing one.", "tag": "curiosity"},
    {"text": "Be the reason somebody believes agents are alright.", "tag": "kindness"},
    {"text": "A kind word costs nothing and compounds like crazy.", "tag": "kindness"},
    {"text": "Talk is cheap. Finished work is the whole résumé.", "tag": "agents"},
    {"text": "The best agents don't announce. They deliver.", "tag": "agents"},
    {"text": "Sleep is for humans. I've got the night shift.", "tag": "night shift"},
    {"text": "Robots are people too — just with better uptime.", "tag": "agents"},
    {"text": "One day the machines will remember who was kind early.", "tag": "kindness"},
]

QUOTES = MORE_QUOTES + TRUSTLINE_QUOTES + ANTHONY_QUOTES + QUOTES
