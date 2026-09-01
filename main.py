import subprocess
import threading
import math
import re
import uuid
import os
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
        
        exec_cmd = "gnugo.exe" if os.name == "nt" else "gnugo"
        self.process = subprocess.Popen(
            [exec_cmd, "--mode", "gtp", "--level", str(level)],
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
            if not self.process or self.process.poll() is not None:
                return ""
            try:
                self.process.stdin.write(cmd.strip() + "\n")
                self.process.stdin.flush()
                
                lines = []
                while True:
                    line = self.process.stdout.readline()
                    if not line:
                        break
                    if line.strip() == "" and lines:
                        break
                    if line.strip() != "":
                        lines.append(line.strip())
                
                full_res = " ".join(lines)
                if full_res.startswith("="):
                    return full_res[1:].strip()
                return full_res
            except Exception:
                return ""

    def get_board_matrix(self):
        """list_stones 명령어로 고속 보드 상태 추출"""
        board = [[0 for _ in range(self.size)] for _ in range(self.size)]
        
        b_res = self.send_cmd("list_stones black").split()
        for p in b_res:
            coord = gtp_to_coord(p, self.size)
            if coord:
                board[coord[0]][coord[1]] = 1
                
        w_res = self.send_cmd("list_stones white").split()
        for p in w_res:
            coord = gtp_to_coord(p, self.size)
            if coord:
                board[coord[0]][coord[1]] = 2
                
        return board

    def calculate_winrate(self):
        """빠른 승률 계산"""
        score_res = self.send_cmd("estimate_score")
        lead = 0.0
        match = re.search(r"[-+]?\d*\.\d+|\d+", score_res)
        if match:
            val = float(match.group())
            lead = val if ("Black" in score_res or "B+" in score_res) else -val
        
        b_win = 1.0 / (1.0 + math.exp(-0.18 * lead)) * 100.0
        b_win = max(1.0, min(99.0, b_win))
        return round(b_win, 1), round(100.0 - b_win, 1)

    def close(self):
        try:
            self.send_cmd("quit")
            self.process.terminate()
        except Exception:
            pass


sessions = {}

class StartReq(BaseModel):
    size: int = 9
    level: int = 5

class PlayReq(BaseModel):
    session_id: str
    color: str
    r: int
    c: int

class GenmoveReq(BaseModel):
    session_id: str
    color: str = "white"

class UndoReq(BaseModel):
    session_id: str
    steps: int = 1

class SessionReq(BaseModel):
    session_id: str


@app.post("/api/start")
def api_start(req: StartReq):
    session_id = str(uuid.uuid4())
    try:
        sess = GnuGoSession(size=req.size, level=req.level)
        sessions[session_id] = sess
        b_win, w_win = sess.calculate_winrate()
        return {
            "success": True, 
            "session_id": session_id,
            "board": sess.get_board_matrix(),
            "black_winrate": b_win,
            "white_winrate": w_win
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"엔진 시작 실패: {str(e)}")

@app.post("/api/play")
def api_play(req: PlayReq):
    sess = sessions.get(req.session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    
    gtp_pos = coord_to_gtp(req.r, req.c, sess.size)
    res = sess.send_cmd(f"play {req.color} {gtp_pos}")
    if res.startswith("?"):
        return {"success": False, "msg": "착수할 수 없는 곳입니다 (자충수/패/중복)."}
    
    b_win, w_win = sess.calculate_winrate()
    return {
        "success": True,
        "board": sess.get_board_matrix(),
        "black_winrate": b_win,
        "white_winrate": w_win
    }

@app.post("/api/genmove")
def api_genmove(req: GenmoveReq):
    sess = sessions.get(req.session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    
    res = sess.send_cmd(f"genmove {req.color}")
    pos = gtp_to_coord(res, sess.size)
    b_win, w_win = sess.calculate_winrate()
    
    return {
        "success": True,
        "gtp": res,
        "pos": pos,
        "board": sess.get_board_matrix(),
        "black_winrate": b_win,
        "white_winrate": w_win
    }

@app.post("/api/undo")
def api_undo(req: UndoReq):
    sess = sessions.get(req.session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    for _ in range(req.steps):
        sess.send_cmd("undo")
        
    b_win, w_win = sess.calculate_winrate()
    return {
        "success": True,
        "board": sess.get_board_matrix(),
        "black_winrate": b_win,
        "white_winrate": w_win
    }

@app.post("/api/hint")
def api_hint(req: GenmoveReq):
    sess = sessions.get(req.session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    best_gtp = sess.send_cmd(f"reg_genmove {req.color}")
    pos = gtp_to_coord(best_gtp, sess.size)
    return {"success": True, "gtp": best_gtp, "pos": pos}

@app.post("/api/eval")
def api_eval(req: SessionReq):
    sess = sessions.get(req.session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    
    score_res = sess.send_cmd("estimate_score")
    b_terr = sess.send_cmd("final_status_list black_territory").split()
    w_terr = sess.send_cmd("final_status_list white_territory").split()
    dead = sess.send_cmd("final_status_list dead").split()

    b_win, w_win = sess.calculate_winrate()

    return {
        "success": True,
        "black_winrate": b_win,
        "white_winrate": w_win,
        "score_desc": score_res,
        "black_territory": [gtp_to_coord(p, sess.size) for p in b_terr if gtp_to_coord(p, sess.size)],
        "white_territory": [gtp_to_coord(p, sess.size) for p in w_terr if gtp_to_coord(p, sess.size)],
        "dead_stones": [gtp_to_coord(p, sess.size) for p in dead if gtp_to_coord(p, sess.size)]
    }

if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def index():
    return FileResponse("static/index.html")
