"""Web Push to the home-screen app. One file per subscription in DATA/push_subs, so
subscribe/unsubscribe (dashboard server) and expiry (any poller) never rewrite a
shared file. The VAPID key is made once by the server process (init) on the volume.
No-op where pywebpush isn't installed (e.g. running the pollers on the Mac).
pywebpush (+cryptography, requests) costs ~30 MB, so it's imported only to send or
to make the key: the 256 MB machine OOM'd with it loaded in every poller."""
import hashlib, importlib.util, json, os, threading, time
from urllib.parse import urlparse

DATA = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
SUBS = f"{DATA}/push_subs"
KEY = f"{DATA}/vapid.pem"
BOOK = {"fanduel": "FanDuel", "draftkings": "DraftKings"}
OUTBOX = f"{DATA}/push_out"   # other processes queue here; the server process sends
CLAIMS = {"sub": os.environ.get("PUSH_SUB", "https://live-betting-value.fly.dev")}
# only real browser push services: the server POSTs to whatever endpoint is stored
PUSH_HOSTS = ("fcm.googleapis.com", ".push.apple.com", ".push.services.mozilla.com", ".notify.windows.com")
MAX_SUBS = 500   # ponytail: the subscribe endpoint is open; cap it instead of adding auth

AVAILABLE = importlib.util.find_spec("pywebpush") is not None

def init(docs):
    """Make the VAPID key if missing; publish its public half for the page."""
    if AVAILABLE:
        os.makedirs(SUBS, exist_ok=True)
    if not AVAILABLE or (os.path.exists(KEY) and os.path.exists(f"{docs}/vapid_public.txt")):
        return
    from py_vapid import Vapid, b64urlencode
    from cryptography.hazmat.primitives import serialization as s
    if not os.path.exists(KEY):
        v = Vapid(); v.generate_keys(); v.save_key(KEY)
    v = Vapid.from_file(KEY)
    pub = b64urlencode(v.public_key.public_bytes(s.Encoding.X962, s.PublicFormat.UncompressedPoint))
    open(f"{docs}/vapid_public.txt", "w").write(pub)

def _path(endpoint):
    return f"{SUBS}/{hashlib.sha256(endpoint.encode()).hexdigest()[:32]}.json"

def subscribe(sub):
    """Store a PushSubscription (its toJSON()). Returns False if it doesn't look like one."""
    ep = sub.get("endpoint", "") if isinstance(sub, dict) else ""
    u = urlparse(ep)
    keys = sub.get("keys") if isinstance(sub, dict) else None
    host = u.hostname or ""
    if (not AVAILABLE or u.scheme != "https" or not isinstance(keys, dict)
            or not keys.get("p256dh") or not keys.get("auth")
            or not any(host == h or (h[0] == "." and host.endswith(h)) for h in PUSH_HOSTS)):
        return False
    if not os.path.exists(_path(ep)) and len(os.listdir(SUBS)) >= MAX_SUBS:
        return False
    json.dump({"endpoint": ep, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}}, open(_path(ep), "w"))
    return True

def unsubscribe(endpoint):
    try:
        os.remove(_path(endpoint))
    except (OSError, TypeError):
        pass

def _send(payload):
    from pywebpush import webpush, WebPushException
    for f in os.listdir(SUBS):
        try:
            sub = json.load(open(f"{SUBS}/{f}"))
            webpush(sub, payload, vapid_private_key=KEY, vapid_claims=dict(CLAIMS), ttl=900, timeout=10)
        except WebPushException as e:
            if e.response is not None and e.response.status_code in (404, 410):
                unsubscribe(sub["endpoint"])   # browser dropped it (app deleted, alerts turned off)
            else:
                print("push error", repr(e)[:200])
        except Exception as e:
            print("push error", repr(e)[:200])

def send(title, body, tag=None, url="./"):
    """Fire-and-forget so a slow push service never delays a poll cycle."""
    if AVAILABLE and os.path.exists(KEY) and os.path.isdir(SUBS):
        payload = json.dumps({"title": title, "body": body, "tag": tag, "url": url})
        threading.Thread(target=_send, args=(payload,), daemon=True).start()

def queue(title, body, tag=None, url="./"):
    """For the spread pollers: leave the message for the server process (drain), so
    they never load pywebpush themselves (~30 MB each on a 256 MB machine)."""
    os.makedirs(OUTBOX, exist_ok=True)
    p = f"{OUTBOX}/{time.time_ns()}.json"
    json.dump({"title": title, "body": body, "tag": tag, "url": url}, open(p + ".tmp", "w"))
    os.replace(p + ".tmp", p)   # drain never sees a half-written file

def drain():
    """Send and clear queued messages. Called by the server process every 10-60s."""
    for f in sorted(os.listdir(OUTBOX)) if os.path.isdir(OUTBOX) else []:
        if f.endswith(".json"):
            try:
                m = json.load(open(f"{OUTBOX}/{f}"))
                if time.time_ns() - int(f[:-5]) < 900e9:   # stale after 15 min (machine was down): drop
                    send(**m)
            except Exception as e:
                print("push queue error", f, repr(e)[:120])
            os.remove(f"{OUTBOX}/{f}")
