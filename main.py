import subprocess
import threading
import math
import re
import uuid
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI()

GTP_LETTERS = "ABCDEFGHJKLMNOPQRST"

def coord_to_gtp(r, c, size):
    return f"{GTP_LETTERS[c]}{size - r}"

def gtp_to_coord(gtp_str, size):
    gtp_str = gtp_str.strip().upper()
    if gtp_str in ("PASS", "RESIGN", ""):
        return None
    try:
        col = GTP_LETTERS.index(gtp_str[0])
        row = size - int(gtp_str[1:])
        return row, col
    except Exception:
        return None


class GnuGoSession:
    def __init__(self, size=9, level=5):
        self.size = size
        self.level = level
        self.lock = threading.Lock()
        self.process = subprocess.Popen(
            ["gnugo", "--mode", "gtp", "--level", str(level)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            bufsize=1
        )
        self.send_cmd(f"boardsize {size}")
        self.send_cmd("clear_board")
        self.send_cmd("komi 6.5")

    def send_cmd(self, cmd):
        with self.lock:
            if not self.process:
                return ""
            self.process.stdin.write(cmd.strip() + "\n")
            self.process.stdin.flush()
            response = []
            while True:
                line = self.process.stdout.readline()
                if line.strip() == "" and response:
                    break
                if line:
                    response.append(line)
            res = "".join(response).strip()
            return res[1:].strip() if res.startswith("=") else res

    def close(self):
        try:
            self.send_cmd("quit")
            self.process.terminate()
        except Exception:
            pass


sessions = {}

class StartReq(BaseModel):
    size: int
    level: int

class PlayReq(BaseModel):
    session_id: str
    color: str
    r: int
    c: int

class SessionReq(BaseModel):
    session_id: str

@app.post("/api/start")
def api_start(req: StartReq):
    session_id = str(uuid.uuid4())
    sessions[session_id] = GnuGoSession(size=req.size, level=req.level)
    return {"session_id": session_id}

@app.post("/api/play")
def api_play(req: PlayReq):
    sess = sessions.get(req.session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    gtp_pos = coord_to_gtp(req.r, req.c, sess.size)
    res = sess.send_cmd(f"play {req.color} {gtp_pos}")
    if res.startswith("?"):
        return {"success": False, "msg": "착수할 수 없는 자리입니다."}
    return {"success": True}

@app.post("/api/genmove")
def api_genmove(req: SessionReq, color: str = "white"):
    sess = sessions.get(req.session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    res = sess.send_cmd(f"genmove {color}")
    pos = gtp_to_coord(res, sess.size)
    return {"gtp": res, "pos": pos}

@app.post("/api/undo")
def api_undo(req: SessionReq, steps: int = 1):
    sess = sessions.get(req.session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    for _ in range(steps):
        sess.send_cmd("undo")
    return {"success": True}

@app.post("/api/hint")
def api_hint(req: SessionReq, color: str = "black"):
    sess = sessions.get(req.session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    best_gtp = sess.send_cmd(f"reg_genmove {color}")
    pos = gtp_to_coord(best_gtp, sess.size)
    return {"gtp": best_gtp, "pos": pos}

@app.post("/api/eval")
def api_eval(req: SessionReq):
    sess = sessions.get(req.session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    
    score_res = sess.send_cmd("estimate_score")
    b_terr = sess.send_cmd("final_status_list black_territory").split()
    w_terr = sess.send_cmd("final_status_list white_territory").split()
    dead = sess.send_cmd("final_status_list dead").split()

    lead = 0.0
    match = re.search(r"[-+]?\d*\.\d+|\d+", score_res)
    if match:
        val = float(match.group())
        lead = val if ("Black" in score_res or "B+" in score_res) else -val
    
    b_win = 1.0 / (1.0 + math.exp(-0.18 * lead)) * 100.0
    b_win = max(1.0, min(99.0, b_win))

    return {
        "black_winrate": round(b_win, 1),
        "white_winrate": round(100.0 - b_win, 1),
        "score_desc": score_res,
        "black_territory": [gtp_to_coord(p, sess.size) for p in b_terr if gtp_to_coord(p, sess.size)],
        "white_territory": [gtp_to_coord(p, sess.size) for p in w_terr if gtp_to_coord(p, sess.size)],
        "dead_stones": [gtp_to_coord(p, sess.size) for p in dead if gtp_to_coord(p, sess.size)]
    }

@app.post("/api/board_state")
def api_board_state(req: SessionReq):
    sess = sessions.get(req.session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    board = []
    for r in range(sess.size):
        row = []
        for c in range(sess.size):
            p = coord_to_gtp(r, c, sess.size)
            col = sess.send_cmd(f"color {p}").lower()
            row.append(1 if "black" in col else (2 if "white" in col else 0))
        board.append(row)
    return {"board": board}

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def index():
    return FileResponse("static/index.html")