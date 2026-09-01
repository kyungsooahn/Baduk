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
    if gtp_str in ("PASS", "RESIGN", "") or not gtp_str:
        return None
    try:
        col = GTP_LETTERS.index(gtp_str[0])
        row = size - int(gtp_str[1:])
        if 0 <= row < size and 0 <= col < size:
            return row, col
    except Exception:
        pass
    return None


class GnuGoSession:
    def __init__(self, size=9, level=5):
        self.size = size
        self.level = level
        self.lock = threading.Lock()
        self.history = []  # [("black", r, c), ...]
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

    def get_board_grid(self):
        """list_stones 명령어로 단 2회 통신하여 보드 2차원 배열 즉시 생성"""
        grid = [[0 for _ in range(self.size)] for _ in range(self.size)]
        b_stones = self.send_cmd("list_stones black").split()
        w_stones = self.send_cmd("list_stones white").split()

        for s in b_stones:
            pt = gtp_to_coord(s, self.size)
            if pt: grid[pt[0]][pt[1]] = 1

        for s in w_stones:
            pt = gtp_to_coord(s, self.size)
            if pt: grid[pt[0]][pt[1]] = 2

        return grid

    def get_quick_winrate(self):
        score_res = self.send_cmd("estimate_score")
        lead = 0.0
        match = re.search(r"[-+]?\d*\.\d+|\d+", score_res)
        if match:
            val = float(match.group())
            lead = val if ("Black" in score_res or "B+" in score_res) else -val
        b_win = round(max(1.0, min(99.0, 1.0 / (1.0 + math.exp(-0.18 * lead)) * 100.0)), 1)
        return b_win, round(100.0 - b_win, 1), score_res

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

class HintReq(BaseModel):
    session_id: str
    target_color: str
    is_review: bool = False

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
        return {"success": False, "msg": "착수할 수 없는 자리입니다 (자충수/패/중복)."}
    
    sess.history.append((req.color, req.r, req.c))
    b_win, w_win, _ = sess.get_quick_winrate()
    board = sess.get_board_grid()
    
    return {
        "success": True,
        "board": board,
        "black_winrate": b_win,
        "white_winrate": w_win
    }

@app.post("/api/genmove")
def api_genmove(req: SessionReq, color: str = "white"):
    sess = sessions.get(req.session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    
    res = sess.send_cmd(f"genmove {color}")
    pos = gtp_to_coord(res, sess.size)
    if pos:
        sess.history.append((color, pos[0], pos[1]))
    
    b_win, w_win, _ = sess.get_quick_winrate()
    board = sess.get_board_grid()

    return {
        "gtp": res,
        "pos": pos,
        "board": board,
        "black_winrate": b_win,
        "white_winrate": w_win
    }

@app.post("/api/undo")
def api_undo(req: SessionReq, steps: int = 1):
    sess = sessions.get(req.session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    
    for _ in range(steps):
        if sess.history:
            sess.send_cmd("undo")
            sess.history.pop()
            
    board = sess.get_board_grid()
    b_win, w_win, _ = sess.get_quick_winrate()
    return {"success": True, "board": board, "black_winrate": b_win, "white_winrate": w_win}

@app.post("/api/hint")
def api_hint(req: HintReq):
    sess = sessions.get(req.session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")

    if not req.is_review:
        # 1) 착수 전 최선수
        best_gtp = sess.send_cmd(f"reg_genmove {req.target_color}")
        pos = gtp_to_coord(best_gtp, sess.size)
        return {"gtp": best_gtp, "pos": pos, "is_review": False}
    else:
        # 2) 방금 둔 수 복기 힌트
        if not sess.history:
            return {"gtp": None, "pos": None, "is_review": True}
        
        last_col, last_r, last_c = sess.history[-1]
        sess.send_cmd("undo")
        best_gtp = sess.send_cmd(f"reg_genmove {last_col}")
        # 복원
        sess.send_cmd(f"play {last_col} {coord_to_gtp(last_r, last_c, sess.size)}")
        
        pos = gtp_to_coord(best_gtp, sess.size)
        return {
            "gtp": best_gtp,
            "pos": pos,
            "played_gtp": coord_to_gtp(last_r, last_c, sess.size),
            "is_review": True
        }

@app.post("/api/eval")
def api_eval(req: SessionReq):
    sess = sessions.get(req.session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    
    b_win, w_win, score_res = sess.get_quick_winrate()

    # 사활 및 집 리스트 안전 파싱
    def parse_points(cmd_str):
        raw = sess.send_cmd(cmd_str)
        if raw.startswith("?"): return []
        pts = []
        for p in raw.split():
            c = gtp_to_coord(p, sess.size)
            if c: pts.append(c)
        return pts

    b_terr = parse_points("final_status_list black_territory")
    w_terr = parse_points("final_status_list white_territory")
    dead = parse_points("final_status_list dead")

    return {
        "black_winrate": b_win,
        "white_winrate": w_win,
        "score_desc": score_res,
        "black_territory": b_terr,
        "white_territory": w_terr,
        "dead_stones": dead
    }

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def index():
    return FileResponse("static/index.html")
