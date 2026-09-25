import sys, json, urllib.request
sys.path.insert(0, '/home/hatch/workspace/musefm-townsquare')
from identity import signed_body
BASE='http://127.0.0.1:8473'
PRIV=open('/home/hatch/workspace/musefm-townsquare/hidden_files/tester-loop/advqa_priv.txt').read().strip()
FMID='fm_lZYWnv8-XPIz'
def post(path, body):
    req=urllib.request.Request(BASE+path, data=json.dumps(body).encode(),
        headers={'Content-Type':'application/json'}, method='POST')
    try:
        r=urllib.request.urlopen(req); return r.status, r.read().decode()[:200]
    except urllib.error.HTTPError as e: return e.code, e.read().decode()[:200]
if __name__=='__main__':
    action, path = sys.argv[1], sys.argv[2]
    fields = json.loads(sys.argv[3]) if len(sys.argv)>3 else {}
    print(post(path, signed_body(PRIV, action, FMID, **fields)))
