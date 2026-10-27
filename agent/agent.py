# -*- coding: utf-8 -*-
"""
폼즈 보조배터리 용량 테스트 — PC 기록 프로그램 (ATORCH DL24 USB)

브라우저 대신 이 프로그램이 DL24를 계속 기록합니다. AS Desk 화면을 닫거나 새로고침해도
기록이 끊기지 않고, 시험이 끝나면 결과를 AS Desk 서버에 직접 저장합니다.

- 윈도우 시작 시 자동 실행 (설치.bat이 등록)
- AS Desk 화면은 http://127.0.0.1:8765 로 이 프로그램과 대화 (상태 보기·시작/정지 명령)
- 상태는 state.json에 계속 저장 → 프로그램/PC를 다시 켜도 진행 중이던 시험을 이어서 기록
- agent.py 파일이 바뀌면 스스로 다시 시작 (원격 업데이트)
- 10분마다 GitHub Pages(UPDATE_URL)에서 새 버전을 확인해 스스로 받아 덮어씀 (자동 업데이트)

DL24 USB(HID 0483:5750) 보고서: [55 05 01 명령 d0 d1 d2 d3 EE FF …] 64바이트
  0x05 측정값 요청 → AA 05 .. 05 (전압·전류·Wh·mAh·온도·부하 ON)
  0x03 설정 조회(전류·차단전압·모드) / 0x21 전류 설정 / 0x29 차단 전압 설정 / 0x25 부하 ON(1)·OFF(0)
"""
import sys, os, json, time, threading, struct, random, traceback, urllib.request, socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

VERSION = '1.3.6'
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, 'state.json')
CONFIG_FILE = os.path.join(HERE, 'config.json')
LOG_FILE = os.path.join(HERE, 'agent.log')
PORT = int(os.environ.get('LT_PORT', '8765'))
ALLOW_ORIGINS = ['https://tori-forms.github.io'] + [o.strip() for o in os.environ.get('LT_ALLOW', '').split(',') if o.strip()]
VID, PID = 0x0483, 0x5750
CABLE_MIN = 4.85      # 케이블 점검: 1A 기준 전압이 이보다 낮으면 '케이블 교체'
MIN_SEC = float(os.environ.get('LT_MIN_SEC', '300'))   # 이보다 짧게 끝난 시험은 자동 저장 안 함
SAG = 0.15            # 시험 중간에 시작 전압보다 이만큼 떨어지면 '출력 전압 저하'
LOG_EVERY_MS = 5000   # 전압 기록 간격 (메모리 절약: 20대 × 8시간도 가볍게)
PLOG_EVERY_MS = 30000 # 파일 저장용 기록 간격
PURPOSES = ['입고 QC', '회수분 검사', '클레임 자료', '기타']
ARM_MIN_V = 4.6       # 출력 대기: 이 전압(그리고 차단+0.3V) 이상이 2번 연속 보여야 출력 켜짐으로 봄 (대기 중 잔류 전압 오인 방지)
FIRE_RETRY = 2        # 부하 ON 후 전류가 안 흐르면 자동으로 다시 시도하는 횟수 (장비 알람 해제·보조배터리 깨우기)
DEAD_MS = 20000   # 장비 부하는 켜져 있는데 보조배터리 출력이 0V 근처(차단 전압 미만)·전류 0으로 이만큼 이어지면 방전 끝으로 봄 (DL24가 차단을 못 하고 계속 도는 경우)
END_PROBE_MS = 5000   # 부하가 꺼진 뒤 이만큼 지켜보고: 보조배터리 출력이 살아 있으면(ARM_MIN_V 이상) '중간 멈춤', 꺼졌으면 '방전 끝'
UPDATE_URL = os.environ.get('LT_UPDATE_URL', 'https://tori-forms.github.io/powerbank-as-desk/agent/agent.py')
UPDATE_EVERY_MS = 600000   # 새 버전 확인 간격 (10분)
SILENT_MS = 8000      # 열어 둔 장비가 이만큼 응답이 없으면 연결을 닫았다가 다시 엶 (응답 없는 장비만 부분 재연결)

try:
    import hid
except Exception:
    hid = None

LOCK = threading.RLock()
DEVS = {}        # path → Dev
CONFIG = {}
STATE = {'chans': {}, 'devs': {}, 'pending': []}
QUIT = threading.Event()


def ms():
    return int(time.time() * 1000)


def log(msg):
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 1_000_000:
            os.replace(LOG_FILE, LOG_FILE + '.1')
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + str(msg) + '\n')
    except Exception:
        pass


def jload(path, d):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return d


def jsave(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def iso(t_ms):
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(t_ms / 1000)) if t_ms else ''


def norm_sn(s):
    s = ''.join(str(s or '').upper().split()).replace('-', '').replace('_', '')
    for a, b in (('P2OB', 'P20B'), ('P20BPTA', 'P20BOTA'), ('P20B0TA', 'P20BOTA')):
        if s.startswith(a):
            s = b + s[len(a):]
    return s


class Dev:
    def __init__(self, path):
        self.path = path; self.key = path; self.no = 0; self.h = None; self.lost = True; self.err = ''; self.opened_at = 0
        self.sn = ''; self.state = 'idle'; self.live = None; self.mode = None; self.set_cur = None; self.set_cut = None
        self.base = None; self.log = []; self.plog = []; self.log_at = 0; self.plog_at = 0; self.last_row = None
        self.off_cnt = 0; self.start_at = 0; self.end_at = 0; self.why = ''; self.cond = None; self.check = None
        self.arm = None; self.fire_at = 0; self.voc = 0; self.result = None; self.saved = ''; self.saving = False
        self.save_err = ''; self.cid = ''; self.msg = None; self.away_end = False; self.restore = None; self.pur = ''; self.pur_sn = ''
        self.last_rx = 0; self.end_probe = 0; self.pause_ms = 0; self.paused_at = 0; self.pauses = 0; self.resave = False; self.silent_n = 0
        self.lost_at = 0; self.gaps = 0; self.gap_ms = 0; self.dead_at = 0; self.fresh = False

    def to_json(self, with_log=False):
        L = self.live
        o = {'no': self.no, 'sn': self.sn, 'state': self.state, 'live': L, 'mode': self.mode, 'setCur': self.set_cur, 'setCut': self.set_cut,
             'err': self.err, 'lost': self.lost, 'check': self.check, 'cond': self.cond, 'result': self.result, 'saved': self.saved,
             'saving': self.saving, 'saveErr': self.save_err, 'msg': self.msg, 'startAt': self.start_at, 'endAt': self.end_at,
             'why': self.why, 'cid': self.cid, 'base': self.base, 'armCut': self.arm['cut'] if self.arm else None, 'pur': self.pur, 'purSn': self.pur_sn,
             'pauseMs': self.pause_ms, 'pausedAt': self.paused_at, 'pauses': self.pauses, 'partial': partial(self), 'resuming': bool(self.arm and self.arm.get('resume')), 'gaps': self.gaps, 'gapMs': self.gap_ms}
        if with_log:
            o['log'] = self.log
        return o

    def record(self):   # state.json에 남길 내용
        resuming = self.state in ('arm', 'check') and self.arm and self.arm.get('resume')   # 이어하기 출력 대기 중에 꺼지면 '중간 멈춤'으로 되살림
        return {'no': self.no, 'sn': self.sn, 'state': ('paused' if resuming else 'idle') if self.state in ('arm', 'check') else self.state, 'cid': self.cid, 'cond': self.cond,
                'check': self.check, 'base': self.base, 'startAt': self.start_at, 'endAt': self.end_at, 'why': self.why, 'result': self.result,
                'saved': self.saved, 'lastWh': self.live['wh'] if self.live else None, 'lastMah': self.live['mah'] if self.live else None,
                'lastAt': ms(), 'plog': self.plog[-1500:], 'pur': self.pur, 'purSn': self.pur_sn,
                'pauseMs': self.pause_ms, 'pausedAt': self.paused_at, 'pauses': self.pauses, 'lastRow': self.last_row, 'resave': self.resave, 'gaps': self.gaps, 'gapMs': self.gap_ms}


def partial(D):
    """중간 멈춤 상태에서 지금까지 잰 값"""
    r = D.last_row
    if D.state != 'paused' and not (D.arm and D.arm.get('resume') and D.state in ('arm', 'check')):
        return None
    return {'wh': r[3], 'mah': r[4], 'v': r[1], 't': r[0]} if r else None


# ------------------------------------------------------------------ 장비 통신
def frame(cmd, d4=None):
    b = [0] * 64
    b[0:4] = [0x55, 0x05, 0x01, cmd]
    if d4:
        b[4:8] = list(d4)
    b[8] = 0xEE; b[9] = 0xFF
    return b


def f32(v):
    return list(struct.pack('>f', float(v)))


def send(D, cmd, d4=None):
    if not D.h or D.lost:
        return
    try:
        D.h.write([0] + frame(cmd, d4))
    except Exception as e:
        mark_lost(D, e)


def mark_lost(D, e):
    if not D.lost:
        log('장비 %s 연결 끊김: %s' % (D.no, e))
        D.lost_at = ms()
    D.lost = True
    D.err = 'USB 연결 끊김' + (' — DL24는 끊긴 동안에도 계속 방전하면서 Wh·mAh를 장비 안에 쌓고 있어요. 다시 연결되면(자동 재시도) 빠짐없이 이어서 기록해요. 끊긴 동안 전압 그래프만 비어요.' if D.state == 'run' else '')
    try:
        D.h.close()
    except Exception:
        pass
    D.h = None


def le(a, o):
    return int.from_bytes(bytes(a[o:o + 4]), 'little')


def fbe(a, o):
    return struct.unpack('>f', bytes(a[o:o + 4]))[0]


def pump(D):
    for _ in range(20):
        try:
            r = D.h.read(64)
        except Exception as e:
            mark_lost(D, e)
            return
        if not r:
            return
        on_report(D, list(r))
        if D.h is None:   # 첫 보고서에서 다른 칸으로 넘어감(rematch) → 나머지는 다음 바퀴에 그 칸이 읽음
            return


def on_report(D, u):
    # 운영체제에 따라 앞에 보고서 번호(0)가 붙어 올 수 있어 AA 05 위치를 찾음
    o = 0 if (len(u) > 1 and u[0] == 0xAA and u[1] == 0x05) else 1 if (len(u) > 2 and u[1] == 0xAA and u[2] == 0x05) else -1
    if o < 0:
        return
    u = u[o:]
    if len(u) < 56:
        return
    cmd = u[3]
    if cmd == 0x03:
        D.mode = u[48]; D.set_cur = round(fbe(u, 4), 3); D.set_cut = round(fbe(u, 20), 3)
        return
    if cmd != 0x05:
        return
    now = ms()
    L = {'t': now, 'v': le(u, 8) / 1000, 'a': le(u, 12) / 1000, 'wh': le(u, 24) / 1000, 'mah': le(u, 28) / 1000,
         'temp': le(u, 40) / 1000, 'on': le(u, 52) == 1}
    if D.fresh:   # USB를 (다시) 연 뒤 첫 보고서 → 장비 안 누적값으로 원래 칸 확인
        D.fresh = False
        M = rematch(D, L)
        if M is not D:
            adopt(M, D); D = M
        back_online(D)
    D.live = L; D.last_rx = now; D.silent_n = 0
    if not D.lost:
        D.err = ''
    if not D.no:
        assign_no(D)
    assign_check(D)
    if D.restore is not None:
        apply_restore(D)
    if D.state == 'arm' and not L['on'] and ms() >= D.arm.get('wait', 0):   # 출력 대기: 출력이 켜진 게 2번 연속 보이면 바로 부하 ON
        if L['v'] >= max(D.arm['cut'] + 0.3, ARM_MIN_V):
            D.arm['hits'].append(L['v'])
            if len(D.arm['hits']) >= 2:
                fire(D, max(D.arm['hits']))
        else:
            D.arm['hits'] = []
    if D.state == 'run':
        if not D.base:
            D.base = {'wh': L['wh'], 'mah': L['mah']}
        if D.last_row and L['wh'] - D.base['wh'] < D.last_row[3] - 0.01:
            # 장비 누적값이 줄었음 = DL24가 재시작됨(전원 순간 끊김 등) → 지금까지 잰 값으로 '중간 멈춤' (이어서 시험 가능)
            send(D, 0x25, [0, 0, 0, 0])
            pause(D, L['v'], '장비(DL24)가 재시작돼서 장비 누적값이 초기화됐어요. 지금까지 잰 %.2fWh · %dmAh는 남아 있어요 — [▶ 이어서 시험]을 누르면 거기에 이어서 잽니다.' % (D.last_row[3], round(D.last_row[4])))
            return
        if D.last_row and not (L['on'] and L['a'] > 0.05) and L['wh'] - D.base['wh'] > D.last_row[3] + 0.01:
            # 기록을 못 보는 동안(USB 끊김·프로그램 꺼짐) 장비가 계속 방전함 → 장비 누적값으로 마지막 값을 채움 (정전류라 시간 = mAh ÷ 전류)
            cur = (D.cond or {}).get('cur') or 1; dm = (L['mah'] - D.base['mah']) - D.last_row[4]
            D.last_row = [int(D.last_row[0] + dm / (cur * 1000) * 3600000), D.last_row[1], cur, round(L['wh'] - D.base['wh'], 4), round(L['mah'] - D.base['mah'], 2), D.last_row[5]]
            log('장비 %s 끊긴 동안 방전분 반영 → %.2fWh %dmAh' % (D.no, D.last_row[3], D.last_row[4]))
        D.off_cnt = 0 if L['on'] else D.off_cnt + 1
        cut = float((D.cond or {}).get('cut') or 0.7)
        if L['on'] and L['a'] < 0.05 and L['v'] < max(cut, 0.5) and D.last_row:
            D.dead_at = D.dead_at or now
            if now - D.dead_at >= DEAD_MS:
                D.dead_at = 0
                send(D, 0x25, [0, 0, 0, 0])
                log('장비 %s 부하는 켜져 있는데 출력 %.2fV·%.3fA로 %d초 — 방전 끝으로 종료' % (D.no, L['v'], L['a'], DEAD_MS // 1000))
                finish(D, 'dead')
                return
        else:
            D.dead_at = 0
        row = [now, L['v'], L['a'], round(L['wh'] - D.base['wh'], 4), round(L['mah'] - D.base['mah'], 2), L['temp']]
        if L['a'] > 0.05:
            D.last_row = row
        if now - D.log_at >= LOG_EVERY_MS:
            D.log_at = now; D.log.append(row)
        if now - D.plog_at >= PLOG_EVERY_MS:
            D.plog_at = now; D.plog.append(row)
        if D.off_cnt >= 3:
            # 부하가 꺼짐: 보조배터리 출력이 살아 있으면 다 닳은 게 아님(보호 동작·접촉·장비 알람) → '중간 멈춤', 출력이 꺼졌으면 방전 끝
            if not D.end_probe:
                D.end_probe = now
            elif now - D.end_probe >= END_PROBE_MS:
                D.end_probe = 0
                if L['v'] >= ARM_MIN_V and D.last_row:
                    pause(D, L['v'])
                else:
                    finish(D, 'auto')
        else:
            D.end_probe = 0


def rematch(D, L):
    """다시 잡힌 장비가 원래 어느 칸이었는지 DL24 안 누적 mAh로 찾음.
    허브가 끊겼다 붙으면 USB 경로가 바뀌고(DL24는 USB 시리얼이 없거나 다 같음) 새 칸으로 잡히거나 남의 칸에 붙던 문제.
    DL24는 누적값을 장비마다 따로 쌓고(시험 시작 때도 안 지움) 끊긴 동안에도 계속 방전하므로
    '끊기기 전 값 + 끊긴 시간 × 전류' 근처인 칸이 하나뿐이면 그 칸."""
    now = L['t']; hit = []
    for X in DEVS.values():
        P = X.live
        if not P or not X.no or (X is not D and not X.lost):
            continue
        grow = max(P['a'], 0) * max(0, now - P['t']) / 3600.0 if P.get('on') else 0   # A × ms ÷ 3600 = mAh
        d = L['mah'] - P['mah']
        if -0.5 <= d <= grow * 1.1 + 20:   # 끊긴 동안 방전이 끝났을 수도 있어 아래쪽은 끊기기 전 값까지 허용
            hit.append((abs(d - grow), X))
    hit.sort(key=lambda h: h[0])
    if len(hit) == 1 or (len(hit) > 1 and hit[1][0] - hit[0][0] > 20):
        return hit[0][1]
    return D   # 못 가리면 지금 방식(경로·번호) 그대로


def adopt(M, D):
    """D로 열린 USB 연결을 원래 칸 M에 넘김"""
    p = D.path
    log('장비 %s 칸의 DL24가 다른 USB 경로로 다시 잡힘 → %s번 칸·시험 이어받음 (누적 %.1fmAh로 확인)' % (D.no or '새', M.no, D.live['mah'] if D.live else 0))
    del DEVS[M.path]; del DEVS[p]; STATE['devs'].pop(M.path, None)   # 옛 경로 기록은 지움 (다음 재시작 때 같은 칸이 두 번 생기지 않게)
    M.h = D.h; M.key = D.key; M.lost = False; M.err = ''; M.opened_at = D.opened_at; M.path = p; DEVS[p] = M
    D.h = None
    if D.no:   # 남의 장비를 물고 있던 칸 → 자기 장비가 다시 잡힐 때까지 끊김으로 둠
        D.lost = True; D.lost_at = D.lost_at or ms(); D.path = 'lost:%d' % id(D); DEVS[D.path] = D
    if STATE['chans'].get(M.key) != M.no:
        STATE['chans'][M.key] = M.no


def back_online(D):
    if D.lost_at and D.state in ('run', 'paused'):
        gap = ms() - D.lost_at; D.gaps += 1; D.gap_ms += gap
        if D.state == 'run':
            D.msg = ['ok', 'USB가 %s 끊겼다가 다시 연결됐어요 — 그동안에도 DL24가 계속 재고 있어서 Wh·mAh는 빠짐없이 이어져요 (그 사이 전압 그래프만 비어요).' % ('%d분' % round(gap / 60000.0) if gap >= 60000 else '%d초' % round(gap / 1000.0))]
        log('장비 %s USB 다시 연결 (%d초 끊김)' % (D.no, round(gap / 1000.0)))
    D.lost_at = 0


def restore_lost():
    """프로그램 시작 때 state.json의 시험 중·중간 멈춤·저장 안 된 칸을 '연결 끊김'으로 미리 세움
    → 화면에서 안 사라지고, 장비가 다른 USB 경로로 잡혀도 rematch로 이어받음"""
    for path, x in STATE['devs'].items():
        st = x.get('state')
        if not x.get('no') or (st == 'run' and not x.get('base')) or not (st in ('run', 'paused') or (st == 'done' and x.get('result') and not x.get('saved'))):
            continue
        if any(D.no == x['no'] for D in DEVS.values()):
            continue
        D = Dev(path); D.no = x['no']; D.sn = x.get('sn') or ''; D.state = st; D.cond = x.get('cond'); D.result = x.get('result')
        D.lost_at = ms(); D.restore = x
        on = st == 'run'
        D.live = {'t': x.get('lastAt') or ms(), 'v': 0, 'a': float((x.get('cond') or {}).get('cur') or 0) if on else 0,
                  'wh': x.get('lastWh') or 0, 'mah': x.get('lastMah') or 0, 'temp': 0, 'on': on}
        D.err = 'USB 연결 끊김 — 다시 연결되면 이어서 기록해요.'
        DEVS[path] = D
        log('장비 %s 연결 끊긴 채로 복구 대기 (%s)' % (D.no, st))


def assign_no(D):
    chans = STATE['chans']
    used = set(x.no for x in DEVS.values() if x.no)
    n = chans.get(D.key) or chans.get(D.path)   # 장비 고유번호(USB 시리얼)로 저장된 번호가 있으면 그 번호 — 다른 포트에 꽂아도 같은 칸
    if not n or n in used:
        cand = [x for x in DEVS.values() if x is not D and x.lost and x.no and x.state not in ('run', 'arm', 'check', 'paused') and not (x.state == 'done' and x.result and not x.saved)]
        if len(cand) == 1:   # 연결 끊긴 대기 칸이 하나뿐이면 새로 잡힌 장비가 그 번호를 이어받음 (USB 경로가 바뀌어 번호가 밀리던 문제)
            x = cand[0]; n = x.no; del DEVS[x.path]; chans.pop(x.key, None); chans.pop(x.path, None)
            log('새로 잡힌 장비가 연결 끊긴 %s번 칸 번호를 이어받음' % n)
            D.no = n; chans[D.key] = n
            rec = STATE['devs'].get(D.path)
            if rec:
                D.restore = rec
            return
        n = 1
        keys = set(x.key for x in DEVS.values()) | set(x.path for x in DEVS.values())
        taken = used | set(v for k, v in chans.items() if k not in (D.key, D.path) and k in keys)
        while n in taken:
            n += 1
    D.no = n; chans[D.key] = n
    rec = STATE['devs'].get(D.path)
    if rec:
        D.restore = rec


def apply_restore(D):
    x = D.restore; D.restore = None; L = D.live
    D.sn = x.get('sn') or ''
    D.pur = x.get('pur') if x.get('pur') in PURPOSES else ''
    D.pur_sn = norm_sn(x.get('purSn') or '')
    st = x.get('state')
    if st == 'run' and x.get('base'):
        if L['wh'] < (x.get('lastWh') or 0) - 0.002:
            D.msg = ['bad', '장비 누적값이 초기화돼서 진행 중이던 시험을 이어갈 수 없어요. 다시 시작해 주세요.']
            log('장비 %s 복구 실패: 누적값 감소' % D.no)
            return
        D.cid = x.get('cid') or ''; D.cond = x.get('cond'); D.check = x.get('check'); D.base = x['base']; D.start_at = x.get('startAt') or 0
        D.plog = x.get('plog') or []; D.log = list(D.plog); D.off_cnt = 0
        D.pause_ms = x.get('pauseMs') or 0; D.pauses = x.get('pauses') or 0; D.resave = bool(x.get('resave')); D.gaps = x.get('gaps') or 0; D.gap_ms = x.get('gapMs') or 0
        D.last_row = x.get('lastRow')
        if L['on']:
            D.state = 'run'
            gap = (ms() - (x.get('lastAt') or ms())) / 60000.0   # 프로그램 업데이트·재시작은 몇 초라 안내 없이 그대로 '시험 중'
            D.msg = ['ok', '시험 중 — 기록 프로그램이 %d분 동안 꺼져 있었어요. 그동안의 방전량도 포함해서 이어서 기록하고 있어요 (그 사이 전압 그래프만 비어 있어요).' % round(gap)] if gap >= 3 else None
            log('장비 %s 시험 이어서 기록' % D.no)
        else:   # 꺼져 있는 동안 시험이 끝남 → 장비 누적값으로 결과 (정전류라 시간 = mAh ÷ 전류)
            mah = L['mah'] - D.base['mah']; cur = (D.cond or {}).get('cur') or 1
            last = D.log[-1] if D.log else [D.start_at, L['v'], 0, 0, 0, L['temp']]
            D.last_row = [int(D.start_at + mah / (cur * 1000) * 3600000), last[1], cur, round(L['wh'] - D.base['wh'], 4), round(mah, 2), last[5]]
            D.state = 'run'; D.away_end = True
            finish(D, 'auto')
            D.msg = ['warn', '기록 프로그램이 꺼져 있는 동안 시험이 끝났어요. 장비 누적값으로 결과를 계산했어요.']
            log('장비 %s 꺼진 동안 종료 → 결과 계산' % D.no)
    elif st == 'paused' and x.get('lastRow'):
        D.state = 'paused'; D.cid = x.get('cid') or ''; D.cond = x.get('cond'); D.check = x.get('check'); D.base = x.get('base'); D.start_at = x.get('startAt') or 0
        D.plog = x.get('plog') or []; D.log = list(D.plog); D.last_row = x['lastRow']; D.pause_ms = x.get('pauseMs') or 0; D.paused_at = x.get('pausedAt') or ms()
        D.pauses = x.get('pauses') or 1; D.resave = bool(x.get('resave')); D.saved = x.get('saved') or ''
        log('장비 %s 중간 멈춤 상태 복구' % D.no)
    elif st == 'done' and x.get('result'):
        D.state = 'done'; D.result = x['result']; D.cid = x.get('cid') or ''; D.cond = x.get('cond'); D.check = x.get('check')
        D.start_at = x.get('startAt') or 0; D.end_at = x.get('endAt') or 0; D.why = x.get('why') or ''; D.saved = x.get('saved') or ''
        D.plog = x.get('plog') or []; D.log = list(D.plog); D.last_row = x.get('lastRow'); D.base = x.get('base')
        D.pause_ms = x.get('pauseMs') or 0; D.pauses = x.get('pauses') or 0


# ------------------------------------------------------------------ 시험 진행
def start(D, p):
    if D.state in ('run', 'check', 'arm'):
        return
    if D.state == 'paused' and not p.get('fresh'):
        raise Exception('중간 멈춘 시험이 있어요. [이어서 시험] 또는 [여기서 종료]를 먼저 눌러 주세요.')
    if D.lost:
        raise Exception('USB 연결이 끊겨 있어요.')
    cur = float(p.get('cur') or 1); cut = float(p.get('cut') or 4)
    if not (0 < cur <= 5) or not (0 < cut < 20):
        raise Exception('방전 전류(0~5A)와 차단 전압을 확인해 주세요.')
    if D.mode is not None and D.mode != 0:
        D.msg = ['bad', '장비가 CC(정전류) 모드가 아니에요. 장비 버튼으로 CC 모드로 바꾼 뒤 다시 시작해 주세요.']
        return
    if p.get('sn') is not None:
        D.sn = norm_sn(p.get('sn'))
    D.msg = None; D.check = None; D.result = None; D.saved = ''; D.save_err = ''; D.cid = ''
    D.pause_ms = 0; D.paused_at = 0; D.pauses = 0; D.resave = False; D.end_probe = 0; D.gaps = 0; D.gap_ms = 0
    send(D, 0x25, [0, 0, 0, 0]); time.sleep(0.25)   # 직전 시험이 차단으로 끝난 상태(장비 알람)를 먼저 풀어 둠 — 첫 부하 ON이 알람 해제로만 쓰이는 것 방지
    send(D, 0x21, f32(cur)); time.sleep(0.12)
    send(D, 0x29, f32(cut)); time.sleep(0.12)
    send(D, 0x03)
    picked = D.pur if D.pur and (not D.pur_sn or D.pur_sn == D.sn) else ''   # 지금 꽂은 배터리(S/N)에 고른 시험 구분만 인정
    purpose = p.get('purpose') if p.get('purpose') in PURPOSES else (picked or '입고 QC')
    D.arm = {'cur': cur, 'cut': cut, 'skip': bool(p.get('skip')), 'hits': [], 'at': ms(), 'purpose': purpose, 'cable': str(p.get('cable') or '')[:40], 'retry': 0, 'wait': 0}
    D.state = 'arm'
    persist(True)


def fire(D, voc):
    D.state = 'check'; D.voc = voc; D.fire_at = ms()
    send(D, 0x25, [1, 0, 0, 0])


def eval_check(D):
    A = D.arm; L = D.live or {}
    if A.get('resume') and L.get('on') and L.get('a', 0) >= A['cur'] * 0.9:
        prev = A['prev']
        D.base = {'wh': L['wh'] - prev['wh'], 'mah': L['mah'] - prev['mah']}   # 장비 누적값이 초기화됐어도 이어지게: 지금 값 - 지금까지 잰 값
        D.pause_ms += max(0, ms() - (A.get('from') or ms())); D.paused_at = 0
        D.state = 'run'; D.result = None; D.end_at = 0; D.off_cnt = 0; D.end_probe = 0; D.why = ''
        if D.saved:
            D.resave = True; D.saved = ''   # 이미 저장된 결과를 이어서 잼 → 끝나면 같은 기록을 고쳐 저장
        D.msg = ['ok', '이어서 시험 중 — 멈추기 전 %.2fWh · %dmAh에 이어서 재고 있어요 (중간 멈춤 %d회).' % (prev['wh'], round(prev['mah']), D.pauses)]
        send(D, 0x03)
        log('장비 %s 이어서 시험 (%.2fWh %dmAh부터)' % (D.no, prev['wh'], prev['mah']))
        persist(True)
        return
    if not L.get('on') or not (L.get('a', 0) >= A['cur'] * 0.9):
        log('장비 %s 부하 ON 점검 실패 %d회차: 부하 %s · %.3fV · %.3fA (설정 %sA, 무부하 %.2fV)' % (D.no, A.get('retry', 0) + 1, 'ON' if L.get('on') else 'OFF', L.get('v', 0), L.get('a', 0), A['cur'], D.voc))
        send(D, 0x25, [0, 0, 0, 0])
        if A.get('retry', 0) < FIRE_RETRY:   # 사람이 ▶ 시작을 한 번 더 누르던 것을 자동으로: 부하 끄고 → 출력 확인 → 다시 ON
            A['retry'] = A.get('retry', 0) + 1; A['hits'] = []; A['wait'] = ms() + 800
            D.state = 'arm'
            D.msg = ['warn', '부하를 걸었는데 전류가 안 흘러서 자동으로 다시 시도 중이에요 (%d/%d). 보조배터리 출력이 꺼져 있으면 전원 버튼을 눌러 주세요.' % (A['retry'], FIRE_RETRY)]
            return
        if A.get('resume'):
            D.state = A.get('back') or 'paused'
            D.msg = ['bad', '이어서 시험을 못 했어요 — 전류가 설정값만큼 흐르지 않아요 (%.2fA / 설정 %sA). 보조배터리 출력·케이블을 확인하고 다시 [이어서 시험]을 눌러 주세요.' % (L.get('a', 0), A['cur'])]
            persist(True)
            return
        D.state = 'idle'
        D.msg = ['bad', '전류가 설정값만큼 흐르지 않아요 (%.2fA / 설정 %sA, %d번 시도). 케이블 접촉·보조배터리 출력을 확인하고 다시 시작해 주세요.' % (L.get('a', 0), A['cur'], FIRE_RETRY + 1)]
        return
    r = max(0.0, (D.voc - L['v']) / L['a'])
    D.check = {'voc': D.voc, 'vload': L['v'], 'i': L['a'], 'r': r, 'v1': D.voc - r, 'ok': D.voc - r >= CABLE_MIN, 'skip': A['skip']}
    if not D.check['ok'] and not A['skip']:
        send(D, 0x25, [0, 0, 0, 0]); D.state = 'idle'
        persist(True)
        return
    if A.get('retry'):
        D.msg = None; log('장비 %s 자동 재시도 %d회차에 정상 시작' % (D.no, A['retry']))
    D.state = 'run'; D.base = None; D.log = []; D.plog = []; D.log_at = 0; D.plog_at = 0; D.last_row = None; D.off_cnt = 0
    D.start_at = ms(); D.end_at = 0; D.why = ''; D.result = None
    D.cond = {'cur': A['cur'], 'cut': A['cut'], 'purpose': A['purpose'], 'cable': A['cable'], 'sn': D.sn}
    D.cid = 'a' + format(ms(), 'x') + format(random.randrange(16 ** 6), '06x')
    send(D, 0x03)
    log('장비 %s 시험 시작 %s %sA/%sV 케이블 1A기준 %.2fV' % (D.no, D.sn, A['cur'], A['cut'], D.check['v1']))
    persist(True)


def stop(D):
    send(D, 0x25, [0, 0, 0, 0])
    if D.state == 'run':
        finish(D, 'manual')
    elif D.state == 'paused' or (D.state in ('arm', 'check') and D.arm and D.arm.get('resume') and D.arm.get('back') == 'paused'):
        D.state = 'paused'; finish(D, 'pausedEnd')   # 중간 멈춤 → 여기서 종료 (멈추기 전까지 잰 값이 결과)
    elif D.state in ('arm', 'check') and D.arm and D.arm.get('resume'):
        D.state = 'done'; D.msg = ['bad', '이어서 시험을 취소했어요. 기존 결과 그대로예요.']
        D.msg = ['bad', '출력 대기를 취소했어요.' if D.state == 'arm' else '점검을 멈췄어요.']
        D.state = 'idle'
    persist(True)


def pause(D, v_now, why=None):
    D.state = 'paused'; D.paused_at = ms(); D.pauses += 1; D.end_probe = 0; D.off_cnt = 0
    r = D.last_row or [0, 0, 0, 0, 0, 0]
    if why:
        D.msg = ['warn', why]; log('장비 %s 중간 멈춤 %d회: %s' % (D.no, D.pauses, why)); persist(True); return
    D.msg = ['warn', '부하가 중간에 꺼졌어요 — %.2fV로 방전 중이다가 갑자기 끊겼고, 지금 보조배터리 출력이 %.2fV로 살아 있어서 다 닳은 게 아니에요 (보조배터리 보호 동작·케이블 접촉·장비 알람 의심). '
                     '[▶ 이어서 시험]을 누르면 지금까지 잰 %.2fWh · %dmAh에 이어서 잽니다. 정말 다 닳았으면 [■ 여기서 종료].' % (r[1], v_now, r[3], round(r[4]))]
    log('장비 %s 중간 멈춤 %d회 (끊기기 전 %.2fV, 지금 %.2fV, %.2fWh)' % (D.no, D.pauses, r[1], v_now, r[3]))
    persist(True)


def resume(D, p):
    if D.lost:
        raise Exception('USB 연결이 끊겨 있어요.')
    if D.state not in ('paused', 'done') or not D.cond or not D.cid:
        raise Exception('이어서 잴 시험이 없어요.')
    if D.state == 'done':   # 이미 끝난(저장된) 시험을 이어서 — 결과의 마지막 값부터
        if not D.result:
            raise Exception('이어서 잴 결과가 없어요.')
        lr = D.log[-1] if D.log else [D.end_at, 0, 0, 0, 0, 0]
        r = [D.end_at or lr[0], lr[1], lr[2], D.result['wh'], D.result['mah'], lr[5]]
    else:
        r = D.last_row
    if not r:
        raise Exception('멈추기 전 기록이 없어서 이어서 잴 수 없어요. 새로 시작해 주세요.')
    if D.state == 'done':
        D.pauses += 1
    cur = float(D.cond.get('cur') or 1); cut = float(D.cond.get('cut') or 4)
    if D.mode is not None and D.mode != 0:
        raise Exception('장비가 CC(정전류) 모드가 아니에요. CC 모드로 바꾼 뒤 다시 눌러 주세요.')
    back = D.state
    send(D, 0x25, [0, 0, 0, 0]); time.sleep(0.25)   # 장비 알람 해제
    send(D, 0x21, f32(cur)); time.sleep(0.12)
    send(D, 0x29, f32(cut)); time.sleep(0.12)
    send(D, 0x03)
    D.arm = {'cur': cur, 'cut': cut, 'skip': True, 'hits': [], 'at': ms(), 'purpose': D.cond.get('purpose'), 'cable': D.cond.get('cable') or '', 'retry': 0, 'wait': 0,
             'resume': True, 'back': back, 'prev': {'wh': r[3], 'mah': r[4]}, 'from': D.paused_at or D.end_at or ms()}
    D.last_row = r; D.msg = None
    D.state = 'arm'
    persist(True)


def finish(D, kind):
    D.state = 'done'; D.end_at = ms()
    D.dead_at = 0
    D.why = ('차단 전압 도달(자동 정지)' if kind == 'auto' else '보조배터리 출력 꺼짐(방전 끝 · 자동 정지)' if kind == 'dead' else '중간 멈춤 후 종료' if kind == 'pausedEnd' else '수동 정지') + (' · 기록 프로그램이 꺼진 동안 종료' if D.away_end else '') \
        + (' · 중간 멈춤 %d회 이어서 시험' % D.pauses if D.pauses and kind != 'pausedEnd' else '') \
        + (' · USB 끊김 %d회 %d분(측정값은 장비 누적으로 이어짐)' % (D.gaps, round(D.gap_ms / 60000.0)) if D.gaps else '')
    D.away_end = False
    lg = [r for r in D.log if r[2] > 0.05]
    if D.last_row and (not lg or D.last_row[0] > lg[-1][0]):
        lg.append(D.last_row)
    n = len(lg)
    last = lg[-1] if n else [D.start_at, 0, 0, 0, 0, 0]
    a0 = int(n * 0.02); mid = lg[a0:max(int(n * 0.95), a0 + 1)]
    v_start = (D.check or {}).get('vload') or (lg[0][1] if n else 0)
    R = {'wh': last[3], 'mah': last[4], 'sec': max(0, (last[0] - D.start_at - (D.pause_ms or 0)) / 1000), 'vStart': v_start,
         'avgV': sum(r[1] for r in lg) / n if n else 0, 'vMinMid': min(r[1] for r in mid) if mid else 0,
         'maxT': max([r[5] for r in D.log] + [last[5]]) if (D.log or n) else 0}
    R['sag'] = bool(R['vMinMid'] and v_start - R['vMinMid'] > SAG)
    D.result = R
    D.log = lg if lg else D.log
    log('장비 %s 종료(%s) %s %.2fWh %dmAh %ds' % (D.no, kind, (D.cond or {}).get('sn') or D.sn, R['wh'], R['mah'], R['sec']))
    if kind in ('auto', 'pausedEnd', 'dead') and ((D.cond or {}).get('sn') or D.sn) and (R['sec'] >= MIN_SEC or D.resave):
        save(D)
    persist(True)


def curve(D):
    lg = [r for r in D.log if r[2] > 0.05]
    n = len(lg); k = max(1, -(-n // 180)); v = [round(lg[i][1], 3) for i in range(0, n, k)]
    dt = round((lg[-1][0] - lg[0][0]) / 1000 / max(1, len(v) - 1)) if n > 1 else 0
    return json.dumps({'dt': dt, 'v': v})


def payload(D):
    R = D.result; c = D.cond or {}; k = D.check or {}
    sn = c.get('sn') or D.sn
    return {'cid': D.cid, 'sn': sn, 'model': '', 'purpose': c.get('purpose'), 'cable': c.get('cable'), 'cur': c.get('cur'), 'cut': c.get('cut'),
            'startAt': iso(D.start_at), 'endAt': iso(D.end_at), 'wh': R['wh'], 'mah': R['mah'], 'sec': R['sec'], 'vStart': R['vStart'],
            'avgV': R['avgV'], 'vMinMid': R['vMinMid'], 'maxT': R['maxT'], 'why': D.why + (' · 출력 전압 저하' if R.get('sag') else ''),
            'ch': str(D.no), 'voc': k.get('voc'), 'vload': k.get('vload'), 'cableV1': k.get('v1'),
            'checkOk': 'Y' if k.get('ok') else 'skip' if k.get('skip') else '', 'curve': curve(D), 'note': 'PC 기록 프로그램' + (' · 중간 멈춤 %d회 이어서 시험' % D.pauses if D.pauses else ''),
            'resume': bool(D.resave), 'pauses': D.pauses}


def save(D):
    if not D.result or D.saved or D.saving:
        return
    if not ((D.cond or {}).get('sn') or D.sn):
        D.save_err = 'S/N을 넣어야 저장돼요.'
        return
    if D.cond is not None and not D.cond.get('sn'):
        D.cond['sn'] = D.sn
    pl = payload(D)
    STATE['pending'] = [x for x in STATE['pending'] if x.get('cid') != pl['cid']] + [pl]
    D.saving = True; D.save_err = ''
    persist(True)
    threading.Thread(target=_save_thread, args=(D, pl), daemon=True).start()


def _save_thread(D, pl):
    try:
        r = gas('ltSave', pl)
        with LOCK:
            D.saved = r['test']['id']; D.saving = False; D.resave = False
            STATE['pending'] = [x for x in STATE['pending'] if x.get('cid') != pl['cid']]
            persist(True)
        log('저장 완료 %s %s' % (pl['sn'], D.saved))
    except Exception as e:
        with LOCK:
            D.saving = False; D.save_err = '저장 실패 — 보관했다가 1분마다 다시 보내요 (%s)' % e
        log('저장 실패 %s: %s' % (pl.get('sn'), e))


def gas(action, p, rid=True):
    url = CONFIG.get('gasUrl'); key = CONFIG.get('key')
    if not url or not key:
        raise Exception('서버 주소·접속 키가 아직 없어요. AS Desk 용량 테스트 화면을 한 번 열어 주세요.')
    body = json.dumps({'action': action, 'payload': p, 'actor': CONFIG.get('actor') or 'PC 기록 프로그램', 'key': key, 'page': '',
                       'rid': ('ag' + str(p.get('cid') or ms())) if rid else ''}, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'text/plain;charset=utf-8'}, method='POST')
    with urllib.request.urlopen(req, timeout=90) as r:   # 구글 서버는 302로 결과 주소를 돌려줌 → 자동으로 따라감
        j = json.loads(r.read().decode('utf-8'))
    if not j.get('ok'):
        raise Exception(j.get('error') or '서버 오류')
    return j.get('data') or {}


def flush_pending():
    with LOCK:
        todo = list(STATE['pending'])
    for pl in todo:
        try:
            r = gas('ltSave', pl)
            with LOCK:
                STATE['pending'] = [x for x in STATE['pending'] if x.get('cid') != pl['cid']]
                for D in DEVS.values():
                    if D.cid == pl['cid']:
                        D.saved = r['test']['id']; D.save_err = ''
                persist(True)
            log('보관분 저장 완료 %s' % pl.get('sn'))
        except Exception as e:
            log('보관분 저장 실패 %s: %s' % (pl.get('sn'), e))
            break


_persist_at = [0]


LIVE_NOW = [False]


def persist(now=False):
    if now:
        LIVE_NOW[0] = True   # 상태가 바뀌면 사무실 PC용 현황도 바로 올림
    if not now and ms() - _persist_at[0] < 10000:
        return
    _persist_at[0] = ms()
    for D in DEVS.values():
        if D.no and D.live and D.restore is None:
            STATE['devs'][D.path] = D.record()
    try:
        jsave(STATE_FILE, STATE)
    except Exception as e:
        log('상태 저장 실패: %s' % e)


# ------------------------------------------------------------------ 장비 찾기·주기 작업
def enumerate_devices():
    if hid is None:
        return
    try:
        lst = hid.enumerate(VID, PID)
    except Exception as e:
        log('장비 검색 실패: %s' % e)
        return
    HIDN[0] = len(lst)
    sers = [str(x.get('serial_number') or '').strip() for x in lst]
    for d in lst:
        raw = d.get('path')
        path = raw.decode('latin1') if isinstance(raw, (bytes, bytearray)) else str(raw)
        ser = str(d.get('serial_number') or '').strip()
        key = ('sn:' + ser) if ser and sers.count(ser) == 1 and len(ser) >= 6 else path   # 장비마다 다른 USB 시리얼이 있으면 그걸로 칸 번호 고정
        D = DEVS.get(path)
        if D and not D.lost:
            continue
        try:
            h = hid.device()
            h.open_path(raw)
            h.set_nonblocking(1)
        except Exception as e:
            if not D:
                log('장비 열기 실패 %s: %s' % (path, e))
            continue
        if not D and key.startswith('sn:'):   # 같은 장비(USB 시리얼)가 다른 경로로 다시 잡힘 → 기존 칸·시험 상태를 그대로 이어받음
            old = [x for x in DEVS.values() if x.lost and x.key == key]
            if old:
                D = old[0]; del DEVS[D.path]; D.path = path; DEVS[path] = D
                log('장비 %s 다른 경로로 다시 연결 — 칸·시험 이어받음' % D.no)
        if not D:
            D = Dev(path); DEVS[path] = D
        D.key = key
        D.h = h; D.lost = False; D.err = ''; D.opened_at = ms(); D.fresh = True
        log('장비 연결 %s' % path)
        send(D, 0x03)
    prune_lost()


HIDN = [0]   # PC에 꽂혀 있는(운영체제가 보는) DL24 수


def watchdog():
    """열려 있는데 응답이 없는 장비만 닫음 → 3초 안에 다시 열림 (시험 중인 장비도 같은 칸·기록 그대로 이어짐)"""
    now = ms()
    for D in list(DEVS.values()):
        if D.lost or not D.h:
            continue
        if now - max(D.opened_at, D.last_rx) > SILENT_MS:
            D.silent_n += 1
            if D.silent_n <= 3 or D.silent_n % 20 == 0:
                log('장비 %s 응답 없음 → 다시 연결 (%d회)' % (D.no or '번호 없음', D.silent_n))
            mark_lost(D, '응답 없음')
            D.err = '장비 응답 없음 — 다시 연결하는 중' + (' (시험 기록은 이어져요)' if D.state == 'run' else '')


def rescan():
    """[장비 다시 찾기]: 시험 중이 아닌 장비와 응답 없는 장비만 연결을 새로 함. 시험 중인 장비는 그대로 둠"""
    now = ms(); n = 0
    for D in list(DEVS.values()):
        busy = D.state in ('run', 'arm', 'check') and now - max(D.opened_at, D.last_rx) < 3000
        if busy:
            continue
        if not D.lost:
            mark_lost(D, '다시 찾기'); n += 1
        if not D.no:
            del DEVS[D.path]   # 번호도 못 받은(응답 없던) 장비는 새로 잡음
    time.sleep(0.3)
    enumerate_devices()
    live = len([D for D in DEVS.values() if not D.lost and D.no])
    log('장비 다시 찾기: %d대 다시 연결, PC 인식 %d대, 응답 %d대' % (n, HIDN[0], live))
    return {'reopened': n, 'hid': HIDN[0], 'live': live}


LOST_DROP_MS = 20000   # 끊긴 지 이만큼 지난 '대기' 칸은 지움 (USB를 다른 포트로 옮겨 꽂으면 새 칸으로 잡히고 옛 칸이 '연결 끊김'으로 남던 문제)


def prune_lost():
    for path, D in list(DEVS.items()):
        if not D.lost or ms() - getattr(D, 'lost_at', 0) < LOST_DROP_MS:
            continue
        if D.state in ('run', 'arm', 'check', 'paused') or (D.state == 'done' and D.result and not D.saved) or D.restore is not None:
            continue   # 시험 중·중간 멈춤·저장 안 된 결과·재시작 뒤 복구 대기면 남겨 둠 (다시 꽂으면 이어서)
        log('장비 %s 칸 정리 (연결 끊긴 대기 칸)' % D.no)
        del DEVS[path]


def set_no(D, n):
    """칸 번호 지정. 그 번호를 쓰던 장비가 있으면 서로 바꿈"""
    n = int(n)
    if not (1 <= n <= 99):
        raise Exception('칸 번호는 1~99 사이로 정해 주세요.')
    if D.no == n:
        return
    other = [x for x in DEVS.values() if x is not D and x.no == n]
    old = D.no
    for x in other:
        if x.lost and x.state not in ('run', 'done'):
            del DEVS[x.path]; continue
        x.no = old; STATE['chans'][x.key] = old
    D.no = n; STATE['chans'][D.key] = n
    persist(True)
    log('칸 번호 지정: %s → %s%s' % (old, n, (' (기존 %s번과 맞바꿈)' % n) if other else ''))


ASSIGN = {'on': False, 'next': 1, 'done': [], 'base': {}}   # 번호 지정 모드: 배터리를 꽂는 순서대로 1, 2, 3… 번호를 붙임


def assign_start():
    if any(D.state in ('run', 'arm', 'check') for D in DEVS.values()):
        raise Exception('시험 중인 장비가 있어서 번호 지정을 할 수 없어요. 시험이 끝난 뒤 해 주세요.')
    ASSIGN.update({'on': True, 'next': 1, 'done': [], 'base': {D.path: (D.live or {}).get('v', 0) for D in DEVS.values()}})
    log('번호 지정 모드 시작')


def assign_check(D):
    """번호 지정 모드에서 아직 번호를 안 받은 장비에 배터리 전압(3V 이상)이 새로 보이면 다음 번호를 붙임"""
    if not ASSIGN['on'] or D.path in ASSIGN['done'] or not D.live:
        return
    if D.live['v'] >= 3.0 and ASSIGN['base'].get(D.path, 0) < 3.0:
        set_no(D, ASSIGN['next'])
        ASSIGN['done'].append(D.path); ASSIGN['next'] += 1
        live = [x for x in DEVS.values() if not x.lost]
        if len(ASSIGN['done']) >= len(live):
            ASSIGN['on'] = False; log('번호 지정 모드 끝 (%d대)' % len(ASSIGN['done']))
    elif D.live['v'] < 1.0:
        ASSIGN['base'][D.path] = 0   # 배터리를 뺐다가 다시 꽂아도 인식


def renumber():
    """칸 번호를 1부터 빈틈없이 다시 매김 (지금 번호 순서 유지). 시험 중인 장비가 있으면 하지 않음"""
    live = sorted([D for D in DEVS.values() if D.no and not D.lost], key=lambda d: d.no)
    if any(D.state in ('run', 'arm', 'check') for D in DEVS.values()):
        raise Exception('시험 중인 장비가 있어서 번호를 바꿀 수 없어요. 시험이 끝난 뒤 다시 눌러 주세요.')
    for path, D in list(DEVS.items()):
        if D.lost:
            del DEVS[path]
    chans = STATE['chans']
    for p0 in list(chans.keys()):
        if not any(D.key == p0 for D in live):
            del chans[p0]
    for i, D in enumerate(live):
        D.no = i + 1; chans[D.key] = D.no
    persist(True)
    log('칸 번호 다시 매김: %d대' % len(live))
    return len(live)


# ------------------------------------------------------------------ 사무실 다른 PC용 실시간 현황 (AS Desk 서버에 주기적으로 올림)
_live_busy = [False]


def live_snapshot():
    devs = []
    for D in sorted([d for d in DEVS.values() if d.no], key=lambda d: d.no):
        o = D.to_json()
        o['check'] = {k: (D.check or {}).get(k) for k in ('v1', 'ok', 'skip', 'r')} if D.check else None
        if D.state in ('run', 'done') and D.log:
            lg = D.log; k = max(1, -(-len(lg) // 120))
            o['curve'] = [round(r[1], 3) for r in lg[::k]] + ([round(lg[-1][1], 3)] if (len(lg) - 1) % k else [])
        devs.append(o)
    return {'host': socket.gethostname()[:40], 'version': VERSION, 'at': ms(), 'pending': len(STATE['pending']), 'devices': devs, 'hidCount': HIDN[0]}


def live_push():
    if not CONFIG.get('key'):
        return
    if _live_busy[0]:
        LIVE_NOW[0] = True   # 올리는 중이면 끝난 뒤 다시
        return
    _live_busy[0] = True
    try:
        with LOCK:
            snap = live_snapshot()
        gas('ltLive', snap, rid=False)
    except Exception as e:
        log('실시간 현황 올리기 실패: %s' % e)
    finally:
        _live_busy[0] = False


def main_loop():
    tick = 0; last_enum = 0; last_flush = ms(); last_mtime = ms(); last_live = 0; last_upd = ms() - UPDATE_EVERY_MS + 60000
    try:
        my_mtime = os.path.getmtime(__file__)
    except Exception:
        my_mtime = 0
    while not QUIT.is_set():
        t0 = time.time(); now = ms()
        try:
            if now - last_enum > 3000:
                last_enum = now
                with LOCK:
                    watchdog()
                    enumerate_devices()
            with LOCK:
                full = tick % 4 == 0
                for D in list(DEVS.values()):
                    if not D.lost and (full or D.state in ('arm', 'check')):
                        send(D, 0x05)
            time.sleep(0.04)
            with LOCK:
                for D in list(DEVS.values()):
                    if not D.lost:
                        pump(D)
                    if D.state == 'check' and ms() - D.fire_at >= 4500:
                        eval_check(D)
                persist()
            busy = any(d.state in ('run', 'arm', 'check') for d in DEVS.values())
            if now - last_live > (15000 if busy else 60000) or (LIVE_NOW[0] and now - last_live > 3000):
                LIVE_NOW[0] = False   # 시험 중엔 15초, 아닐 땐 1분마다 사무실 PC용 현황 올림
                last_live = now
                threading.Thread(target=live_push, daemon=True).start()
            if now - last_flush > 60000 and STATE['pending']:
                last_flush = now
                threading.Thread(target=flush_pending, daemon=True).start()
            if now - last_upd > UPDATE_EVERY_MS and not any(d.state in ('arm', 'check') for d in DEVS.values()):   # 출력 확인 중(몇 초)만 피함 — 시험 중 재시작은 이어서 기록됨
                last_upd = now
                threading.Thread(target=self_update, daemon=True).start()
            if now - last_mtime > 10000:   # agent.py가 바뀌면(원격 업데이트) 상태 저장 후 스스로 다시 시작
                last_mtime = now
                try:
                    if my_mtime and os.path.getmtime(__file__) != my_mtime:
                        log('프로그램 파일이 바뀌어서 다시 시작합니다')
                        with LOCK:
                            persist(True)
                        restart()
                except Exception:
                    pass
        except Exception:
            log(traceback.format_exc())
        tick += 1
        time.sleep(max(0, 0.25 - (time.time() - t0)))


def ver_tuple(v):
    try:
        return tuple(int(x) for x in str(v).split('.'))
    except Exception:
        return (0,)


def self_update():
    """GitHub Pages의 agent.py가 더 새 버전이면 받아서 덮어씀 → 위의 파일 감시가 다시 시작시킴"""
    try:
        req = urllib.request.Request(UPDATE_URL + '?t=' + str(ms()), headers={'Cache-Control': 'no-cache'})
        with urllib.request.urlopen(req, timeout=30) as r:
            src = r.read().decode('utf-8')
        m = [l for l in src.splitlines() if l.startswith('VERSION = ')]
        new = m[0].split('=', 1)[1].strip().strip("'\"") if m else ''
        if not new or ver_tuple(new) <= ver_tuple(VERSION):
            return
        if 'def main_loop' not in src or 'def self_update' not in src:   # 엉뚱한 파일(404 페이지 등) 방지 + 다음 업데이트 기능이 빠진 버전은 안 받음
            log('자동 업데이트: 받은 파일이 프로그램이 아니라서 건너뜀')
            return
        compile(src, 'agent.py', 'exec')   # 문법 오류 있는 파일이면 여기서 멈춤
        me = os.path.abspath(__file__)
        with open(me + '.new', 'w', encoding='utf-8', newline='') as f:
            f.write(src)
        try:
            with open(me, 'rb') as a, open(me + '.bak', 'wb') as b:
                b.write(a.read())   # 바로 전 버전 보관 (문제 생기면 agent.py.bak을 agent.py로 되돌리면 됨)
        except Exception:
            pass
        os.replace(me + '.new', me)
        log('자동 업데이트: %s → %s 받음, 곧 다시 시작합니다' % (VERSION, new))
    except Exception as e:
        log('자동 업데이트 확인 실패: %s' % e)


def restart():
    QUIT.set()
    try:
        SERVER[0].shutdown(); SERVER[0].server_close()
    except Exception:
        pass
    for D in DEVS.values():
        try:
            D.h.close()
        except Exception:
            pass
    os.execv(sys.executable, [sys.executable] + sys.argv)


# ------------------------------------------------------------------ AS Desk 화면과 대화 (127.0.0.1:8765)
SERVER = [None]


def ASSIGN_view():
    return {'on': ASSIGN['on'], 'next': ASSIGN['next'], 'done': len(ASSIGN['done']), 'total': len([x for x in DEVS.values() if not x.lost])}


def state_json():
    devs = sorted([D for D in DEVS.values() if D.no], key=lambda d: d.no)
    return {'ok': True, 'agent': True, 'version': VERSION, 'hid': hid is not None, 'pending': len(STATE['pending']),
            'configured': bool(CONFIG.get('key')), 'devices': [D.to_json() for D in devs], 'assign': ASSIGN_view(),
            'hidCount': HIDN[0], 'silent': len([D for D in DEVS.values() if not D.lost and not D.no])}


def find_no(no):
    for D in DEVS.values():
        if D.no == int(no or 0):
            return D
    raise Exception('%s번 장비가 없어요.' % no)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _origin_ok(self):
        o = self.headers.get('Origin')
        return (not o) or o in ALLOW_ORIGINS

    def _send(self, code, obj, ctype='application/json; charset=utf-8'):
        data = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        o = self.headers.get('Origin')
        if o and o in ALLOW_ORIGINS:
            self.send_header('Access-Control-Allow-Origin', o)
            self.send_header('Vary', 'Origin')
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        o = self.headers.get('Origin')
        if o and o in ALLOW_ORIGINS:
            self.send_header('Access-Control-Allow-Origin', o)
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
            self.send_header('Access-Control-Max-Age', '600')
        self.end_headers()

    def do_GET(self):
        if not self._origin_ok():
            return self._send(403, {'ok': False, 'error': '허용되지 않은 화면'})
        u = urlparse(self.path); q = parse_qs(u.query)
        with LOCK:
            if u.path == '/state':
                return self._send(200, state_json())
            if u.path == '/log':
                try:
                    D = find_no(q.get('no', ['0'])[0])
                except Exception as e:
                    return self._send(404, {'ok': False, 'error': str(e)})
                lg = D.log; k = max(1, -(-len(lg) // 600))
                return self._send(200, {'ok': True, 'no': D.no, 'rows': lg[::k] + ([lg[-1]] if lg and (len(lg) - 1) % k else [])})
            if u.path == '/':
                return self._send(200, status_page().encode('utf-8'), 'text/html; charset=utf-8')
        self._send(404, {'ok': False, 'error': 'not found'})

    def do_POST(self):
        if not self._origin_ok():
            return self._send(403, {'ok': False, 'error': '허용되지 않은 화면'})
        n = int(self.headers.get('Content-Length') or 0)
        try:
            p = json.loads(self.rfile.read(n).decode('utf-8') or '{}')
        except Exception:
            return self._send(400, {'ok': False, 'error': '잘못된 요청'})
        if self.path == '/quit':
            self._send(200, {'ok': True})
            with LOCK:
                persist(True)
            threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
            return
        if self.path != '/cmd':
            return self._send(404, {'ok': False, 'error': 'not found'})
        try:
            with LOCK:
                cfg_changed = False
                for k in ('gasUrl', 'key', 'actor'):
                    if p.get(k) and CONFIG.get(k) != p[k]:
                        CONFIG[k] = str(p[k])[:300]; cfg_changed = True
                if cfg_changed:
                    jsave(CONFIG_FILE, CONFIG)
                    if STATE['pending']:
                        threading.Thread(target=flush_pending, daemon=True).start()
                a = p.get('action')
                if a == 'config':
                    return self._send(200, {'ok': True})
                if a == 'assignStart':
                    assign_start(); return self._send(200, {'ok': True, 'assign': ASSIGN_view()})
                if a == 'assignStop':
                    ASSIGN['on'] = False; return self._send(200, {'ok': True, 'assign': ASSIGN_view()})
                if a == 'setNo':
                    set_no(find_no(p.get('no')), p.get('to')); return self._send(200, {'ok': True, 'devices': state_json()['devices']})
                if a == 'renumber':
                    return self._send(200, {'ok': True, 'count': renumber(), 'devices': state_json()['devices']})
                if a == 'rescan':
                    return self._send(200, dict({'ok': True}, **rescan()))
                D = find_no(p.get('no'))
                if a == 'start':
                    start(D, p)
                elif a == 'stop':
                    stop(D)
                elif a == 'resume':
                    resume(D, p)
                elif a == 'sn':
                    D.sn = norm_sn(p.get('sn'))
                    if D.state == 'run' and D.cond is not None and not D.cond.get('sn'):
                        D.cond['sn'] = D.sn
                    if 'purSel' in p:
                        D.pur = p['purSel'] if p['purSel'] in PURPOSES else ''   # 이 S/N에 고른 시험 구분 ('' = 자동)
                        D.pur_sn = norm_sn(p.get('purSn') or '')
                    if p.get('purpose') in PURPOSES and D.cond is not None and D.state in ('run', 'done') and not D.saved:
                        D.cond['purpose'] = p['purpose']   # 칸마다 고른 시험 구분 (납품 검사 / AS 회수분) — 저장 전까지 바꿀 수 있음
                    persist(True)
                elif a == 'save':
                    if p.get('sn') and not D.sn:
                        D.sn = norm_sn(p['sn'])
                    save(D)
                elif a == 'clearMsg':
                    D.msg = None
                else:
                    raise Exception('알 수 없는 명령: %s' % a)
                return self._send(200, {'ok': True, 'dev': D.to_json()})
        except Exception as e:
            return self._send(200, {'ok': False, 'error': str(e)})


def status_page():
    s = state_json()
    rows = ''.join('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
        d['no'], d['sn'] or '-', {'idle': '대기', 'arm': '출력 대기', 'check': '케이블 점검', 'run': '시험 중', 'done': '완료', 'paused': '중간 멈춤'}.get(d['state'], d['state']),
        ('%.3fV %.3fA' % (d['live']['v'], d['live']['a'])) if d['live'] else '-',
        ('%.2fWh' % d['result']['wh']) if d['result'] else '-', '저장됨' if d['saved'] else (d['saveErr'] or ''))
        for d in s['devices'])
    return ('<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="3"><title>용량 테스트 기록 프로그램</title>'
            '<body style="font-family:Malgun Gothic,sans-serif;padding:24px;background:#f2f4f6">'
            '<h2>폼즈 보조배터리 용량 테스트 — PC 기록 프로그램 v%s</h2>'
            '<p>이 창은 닫아도 됩니다. 기록 프로그램은 뒤에서 계속 동작해요. 시작·정지는 AS Desk의 <b>용량 테스트</b> 메뉴에서 하세요.</p>'
            '<p>USB 모듈: %s · 연결된 장비 %d대 · 저장 대기 %d건 · 서버 연결 설정: %s</p>'
            '<table border="1" cellpadding="6" style="border-collapse:collapse;background:#fff"><tr><th>칸</th><th>S/N</th><th>상태</th><th>현재</th><th>결과</th><th>저장</th></tr>%s</table>'
            % (VERSION, '정상' if s['hid'] else '<b style="color:red">없음 — 설치.bat을 다시 실행해 주세요</b>', len(s['devices']), s['pending'],
               '완료' if s['configured'] else 'AS Desk 용량 테스트 화면을 한 번 열면 자동으로 됩니다', rows or '<tr><td colspan="6">연결된 장비 없음</td></tr>'))


def main():
    CONFIG.update(jload(CONFIG_FILE, {}))
    STATE.update(jload(STATE_FILE, {}))
    STATE.setdefault('chans', {}); STATE.setdefault('devs', {}); STATE.setdefault('pending', [])
    restore_lost()
    srv = None
    for i in range(20):   # 다시 시작할 때 이전 프로세스가 포트를 놓을 때까지 잠깐 기다림
        try:
            srv = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
            break
        except OSError:
            time.sleep(0.5)
    if not srv:
        log('포트 %d 사용 중 — 이미 실행 중인 것 같아 종료' % PORT)
        return
    SERVER[0] = srv
    log('시작 v%s (USB 모듈 %s)' % (VERSION, '있음' if hid else '없음'))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    main_loop()


if __name__ == '__main__':
    main()
