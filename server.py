from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from threading import RLock, Timer
from pathlib import Path
from urllib.parse import urlsplit
import json, secrets, string, time, os

ROOT = Path(__file__).parent
ROOMS = {}
LOCK = RLock()
PICK_SECONDS = 5
PREPARE_SECONDS = 3
PREP_CUE_SECONDS = 0.65
REVEAL_SECONDS = 2
MAX_BODY = 32 * 1024
ROOM_TTL = 6 * 60 * 60
SCORE_OPTIONS = (5, 10, 15)
TEAM_OPTIONS = tuple(range(2, 11))

class GameError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status

def make_code():
    return ''.join(secrets.choice(string.ascii_uppercase + '23456789') for _ in range(5))

def player_by_token(room, token):
    if not isinstance(token, str) or not token:
        return None
    return next((p for p in room['players'] if secrets.compare_digest(p['token'], token)), None)

def prune_rooms(now=None):
    now = time.time() if now is None else now
    stale = [key for key, room in ROOMS.items() if now - room['lastActivity'] > ROOM_TTL]
    for key in stale:
        del ROOMS[key]

def touch(room, player=None):
    now = time.time()
    room['lastActivity'] = now
    room['stateVersion'] = room.get('stateVersion', 0) + 1
    if player:
        player['lastSeen'] = now
    host = next((p for p in room['players'] if p['id'] == room['hostId']), None)
    if host and now - host['lastSeen'] > 30:
        replacement = next((p for p in room['players'] if now - p['lastSeen'] <= 30), None)
        if replacement:
            room['hostId'] = replacement['id']

def next_alive(room, team):
    return next((p for p in sorted(room['players'], key=lambda x: x['position'])
                 if p['team'] == team and p['alive']), None)

def active_player(room, player_id):
    return next((p for p in room['players'] if p['id'] == player_id), None)

def teams_full(room):
    return all(sum(p['team'] == team for p in room['players']) == room['teamSize'] for team in ('A', 'B'))

def begin_turn(room, attacker, defender):
    if not attacker or not defender or attacker['id'] == defender['id']:
        raise GameError('A valid attacker and a different defender are required.', 409)
    if not attacker['alive'] or not defender['alive']:
        raise GameError('Eliminated players cannot take part.', 409)
    room['attacker'] = attacker['id']
    room['defender'] = defender['id']
    room['round'] += 1
    room['roundId'] = secrets.token_urlsafe(12)
    room['picks'] = {}
    room['settledRoundId'] = None
    room['status'] = 'preparing'
    room['prepCue'] = False
    room['deadlineMono'] = time.monotonic() + PREPARE_SECONDS
    room['deadlineEpochMs'] = int(time.time() * 1000) + PREPARE_SECONDS * 1000
    timer = Timer(PREPARE_SECONDS, begin_selection, args=(room['code'], room['roundId']))
    timer.daemon = True
    timer.start()

def begin_selection(room_code, round_id):
    with LOCK:
        room = ROOMS.get(room_code)
        if not room or room['roundId'] != round_id or room['status'] != 'preparing' or room.get('prepCue'):
            return False
        room['prepCue'] = True
        room['deadlineMono'] = time.monotonic() + PREP_CUE_SECONDS
        room['deadlineEpochMs'] = int(time.time() * 1000) + int(PREP_CUE_SECONDS * 1000)
        touch(room)
        timer = Timer(PREP_CUE_SECONDS, start_choosing, args=(room_code, round_id))
        timer.daemon = True
        timer.start()
        return True

def start_choosing(room_code, round_id):
    with LOCK:
        room = ROOMS.get(room_code)
        if not room or room['roundId'] != round_id or room['status'] != 'preparing' or not room.get('prepCue'):
            return False
        room['status'] = 'choosing'
        room['prepCue'] = False
        room['deadlineMono'] = time.monotonic() + PICK_SECONDS
        room['deadlineEpochMs'] = int(time.time() * 1000) + PICK_SECONDS * 1000
        touch(room)
        timer = Timer(PICK_SECONDS, resolve_timeout, args=(room_code, round_id))
        timer.daemon = True
        timer.start()
        return True

def finish_reveal(room_code, round_id):
    with LOCK:
        room = ROOMS.get(room_code)
        if not room or room['roundId'] != round_id or room['status'] != 'revealing':
            return False
        room['status'] = 'finished' if room.get('winner') is not None else 'round_complete'
        room['deadlineMono'] = None
        room['deadlineEpochMs'] = None
        touch(room)
        return True

def schedule_reveal_end(room):
    room['status'] = 'revealing'
    room['deadlineMono'] = time.monotonic() + REVEAL_SECONDS
    room['deadlineEpochMs'] = int(time.time() * 1000) + REVEAL_SECONDS * 1000
    timer = Timer(REVEAL_SECONDS, finish_reveal, args=(room['code'], room['roundId']))
    timer.daemon = True
    timer.start()

def finish_if_team(room):
    alive = {team: any(p['team'] == team and p['alive'] for p in room['players']) for team in ('A', 'B')}
    if not alive['A'] or not alive['B']:
        room['winner'] = 'A' if alive['A'] else 'B'
        return True
    return False

def resolve_round(room, timeout=False):
    if room['status'] != 'choosing' or room.get('settledRoundId') == room.get('roundId'):
        return False
    attacker = active_player(room, room['attacker'])
    defender = active_player(room, room['defender'])
    if not attacker or not defender or attacker['id'] == defender['id']:
        return False
    picks = room['picks']
    for player in (attacker, defender):
        if player['id'] not in picks:
            picks[player['id']] = {'foot': secrets.choice(('LEFT', 'RIGHT')), 'source': 'automatic'}
    attacker_pick = picks[attacker['id']]
    defender_pick = picks[defender['id']]
    outcome = 'SYNC' if attacker_pick['foot'] == defender_pick['foot'] else 'SWITCH'
    winner_team = 'A' if outcome == 'SYNC' else 'B'
    winner = attacker if attacker['team'] == winner_team else defender
    loser = defender if winner is attacker else attacker
    room['settledRoundId'] = room['roundId']
    result = {
        'round': room['round'], 'roundId': room['roundId'],
        'attacker': attacker['id'], 'defender': defender['id'],
        'attackerFoot': attacker_pick['foot'], 'defenderFoot': defender_pick['foot'],
        'attackerSource': attacker_pick['source'], 'defenderSource': defender_pick['source'],
        'outcome': outcome, 'winnerTeam': winner_team, 'winner': winner['id'], 'loser': loser['id'],
        'timeout': bool(timeout),
        'message': f"{outcome} ({'Opapa' if outcome == 'SYNC' else 'Osheshe'}) wins: {winner['name']} takes the exchange."
    }
    if room['mode'] == 'duel':
        winner['score'] += 1
        result['message'] = f"{outcome} ({'Opapa' if outcome == 'SYNC' else 'Osheshe'}) wins. {winner['name']} scores {winner['score']} of {room['targetScore']}."
        room['lastResult'] = result
        room['events'].append(result.copy())
        if winner['score'] >= room['targetScore']:
            room['winner'] = winner['id']
        else:
            opponent = defender if winner['id'] == attacker['id'] else attacker
            room['attacker'], room['defender'] = winner['id'], opponent['id']
        schedule_reveal_end(room)
        return True

    loser['alive'] = False
    room['eliminationStreak'][winner['team']] += 1
    room['eliminationStreak'][loser['team']] = 0
    result['message'] = f"{outcome} ({'Opapa' if outcome == 'SYNC' else 'Osheshe'}) wins. {loser['name']} is eliminated."
    room['lastResult'] = result
    room['events'].append(result.copy())
    if finish_if_team(room):
        result['message'] += f" Team {room['winner']} wins the game!"
        room['events'][-1] = result.copy()
        room['lastResult'] = result
        schedule_reveal_end(room)
        return True
    opponent = next_alive(room, loser['team'])
    if not opponent or opponent['id'] == winner['id']:
        room['winner'] = winner['team']
        result['message'] += f" Team {room['winner']} wins the game!"
        room['events'][-1] = result.copy()
        room['lastResult'] = result
    else:
        room['attacker'], room['defender'] = winner['id'], opponent['id']
    schedule_reveal_end(room)
    return True

def resolve_timeout(room_code, round_id):
    with LOCK:
        room = ROOMS.get(room_code)
        if room and room['roundId'] == round_id and room['status'] == 'choosing':
            resolve_round(room, timeout=True)
            touch(room)

def submit_pick(room, player, foot, round_id):
    if room['status'] != 'choosing':
        raise GameError('There is no active foot choice.', 409)
    if not isinstance(round_id, str) or round_id != room['roundId']:
        raise GameError('This choice belongs to an earlier round.', 409)
    if time.monotonic() >= room['deadlineMono']:
        resolve_round(room, timeout=True)
        raise GameError('The five-second deadline passed. The server resolved the round.', 409)
    if player['id'] not in (room['attacker'], room['defender']):
        raise GameError('You are watching this exchange, not playing in it.', 403)
    if foot not in ('LEFT', 'RIGHT'):
        raise GameError('Pick LEFT or RIGHT.', 400)
    if player['id'] in room['picks']:
        raise GameError('Your foot is already locked for this round.', 409)
    room['picks'][player['id']] = {'foot': foot, 'source': 'manual'}
    if len(room['picks']) == 2:
        resolve_round(room)
    return True

def room_snapshot(room, token):
    me = player_by_token(room, token)
    if not me:
        raise GameError('This player session is not valid for this room.', 401)
    touch(room, me)
    now_mono = time.monotonic()
    timed_states = ('preparing', 'choosing', 'revealing')
    remaining = max(0, int((room['deadlineMono'] - now_mono) * 1000)) if room['status'] in timed_states and room['deadlineMono'] else 0
    player_rows = []
    for p in room['players']:
        player_rows.append({
            'id': p['id'], 'name': p['name'], 'team': p['team'], 'teamName': 'SYNC' if p['team'] == 'A' else 'SWITCH',
            'alive': p['alive'], 'score': p['score'], 'position': p['position'],
            'isHost': p['id'] == room['hostId'], 'connected': time.time() - p['lastSeen'] <= 30
        })
    locked = room['picks']
    return {
        'code': room['code'], 'stateVersion': room['stateVersion'], 'mode': room['mode'], 'teamSize': room['teamSize'],
        'targetScore': room['targetScore'], 'status': room['status'], 'players': player_rows,
        'you': me['id'], 'isHost': me['id'] == room['hostId'], 'hostId': room['hostId'],
        'attacker': room.get('attacker'), 'defender': room.get('defender'),
        'round': room['round'], 'roundId': room.get('roundId'), 'prepCue': room.get('prepCue', False), 'deadlineEpochMs': room.get('deadlineEpochMs'),
        'serverNowMs': int(time.time() * 1000), 'remainingMs': remaining,
        'attackerLocked': room.get('attacker') in locked, 'defenderLocked': room.get('defender') in locked,
        'yourFoot': locked.get(me['id'], {}).get('foot'),
        'youLocked': me['id'] in locked, 'lastResult': room.get('lastResult'), 'winner': room.get('winner'),
        'events': room['events'][-24:], 'eliminationStreak': room['eliminationStreak'].copy(),
        'playersNeeded': (2 - len(room['players'])) if room['mode'] == 'duel' else max(0, room['teamSize']*2 - len(room['players'])),
        'teamsFull': teams_full(room) if room['mode'] == 'teams' else len(room['players']) == 2
    }

def make_player(room, name, team):
    ids = {p['id'] for p in room['players']}
    player_id = secrets.token_urlsafe(9)
    while player_id in ids:
        player_id = secrets.token_urlsafe(9)
    position = sum(p['team'] == team for p in room['players'])
    player = {
        'id': player_id, 'token': secrets.token_urlsafe(32), 'name': str(name or 'Player').strip()[:18] or 'Player',
        'team': team, 'alive': True, 'score': 0, 'position': position, 'lastSeen': time.time()
    }
    room['players'].append(player)
    return player

def new_room(room_code, mode, team_size, target_score):
    now = time.time()
    return {
        'code': room_code, 'stateVersion': 0, 'mode': mode, 'teamSize': team_size, 'targetScore': target_score,
        'status': 'lobby', 'players': [], 'events': [], 'round': 0, 'roundId': None, 'prepCue': False,
        'settledRoundId': None, 'winner': None, 'hostId': None, 'attacker': None, 'defender': None,
        'picks': {}, 'lastResult': None, 'deadlineMono': None, 'deadlineEpochMs': None,
        'eliminationStreak': {'A': 0, 'B': 0}, 'createdAt': now, 'lastActivity': now, 'gameNumber': 0
    }

def start_game(room):
    if room['mode'] == 'duel':
        if len(room['players']) != 2:
            raise GameError('A 1v1 game needs exactly two players.', 409)
    elif not teams_full(room):
        raise GameError(f"Both teams need exactly {room['teamSize']} players before starting.", 409)
    for p in room['players']:
        p['alive'] = True
        p['score'] = 0
    room['events'] = []
    room['winner'] = None
    room['lastResult'] = None
    room['round'] = 0
    room['prepCue'] = False
    room['eliminationStreak'] = {'A': 0, 'B': 0}
    room['gameNumber'] += 1
    if room['mode'] == 'duel':
        begin_turn(room, room['players'][0], room['players'][1])
    else:
        begin_turn(room, next_alive(room, 'A'), next_alive(room, 'B'))

def reset_to_lobby(room):
    for p in room['players']:
        p['alive'] = True
        p['score'] = 0
    room['status'] = 'lobby'
    room['winner'] = None
    room['attacker'] = None
    room['defender'] = None
    room['deadlineMono'] = None
    room['deadlineEpochMs'] = None
    room['lastResult'] = None
    room['picks'] = {}
    room['roundId'] = None
    room['settledRoundId'] = None
    room['events'] = []
    room['eliminationStreak'] = {'A': 0, 'B': 0}
    room['round'] = 0

def parse_json_body(body):
    if len(body) > MAX_BODY:
        raise GameError('Request body is too large.', 413)
    try:
        data = json.loads(body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GameError('Invalid JSON request.', 400)
    if not isinstance(data, dict):
        raise GameError('Request must be a JSON object.', 400)
    return data

def create_session(data):
    mode = data.get('mode', 'duel')
    if mode not in ('duel', 'teams'):
        raise GameError('Choose 1v1 or Team Elimination.')
    raw_size = data.get('teamSize', 2)
    try:
        team_size = int(raw_size)
    except (TypeError, ValueError):
        raise GameError('Team size must be from 2 to 10.')
    if team_size not in TEAM_OPTIONS or str(team_size) != str(raw_size):
        raise GameError('Team size must be from 2 to 10.')
    raw_target = data.get('targetScore', 10)
    try:
        target = int(raw_target)
    except (TypeError, ValueError):
        raise GameError('Winning score must be 5, 10, or 15.')
    if target not in SCORE_OPTIONS or str(target) != str(raw_target):
        raise GameError('Winning score must be 5, 10, or 15.')
    room_code = make_code()
    while room_code in ROOMS:
        room_code = make_code()
    room = new_room(room_code, mode, team_size, target)
    ROOMS[room_code] = room
    player = make_player(room, data.get('name'), 'A')
    room['hostId'] = player['id']
    snapshot = room_snapshot(room, player['token'])
    return {'token': player['token'], 'roomCode': room_code, 'state': snapshot}

def join_session(data):
    code = str(data.get('code', '')).strip().upper()
    room = ROOMS.get(code)
    if not room:
        raise GameError('Room not found. Check the code and try again.', 404)
    if room['status'] != 'lobby':
        raise GameError('This game has already started.', 409)
    capacity = 2 if room['mode'] == 'duel' else room['teamSize'] * 2
    if len(room['players']) >= capacity:
        raise GameError('This room is full.', 409)
    if room['mode'] == 'duel':
        team = 'B'
    else:
        counts = {t: sum(p['team'] == t for p in room['players']) for t in ('A', 'B')}
        team = min(('A', 'B'), key=lambda t: (counts[t], 0 if t == 'A' else 1))
        if counts[team] >= room['teamSize']:
            raise GameError('That team has reached capacity.', 409)
    player = make_player(room, data.get('name'), team)
    touch(room, player)
    return {'token': player['token'], 'state': room_snapshot(room, player['token'])}

def player_action(code, action, data):
    room = ROOMS.get(code)
    if not room:
        raise GameError('This room no longer exists. Ask the host for a new room link.', 404)
    player = player_by_token(room, data.get('token'))
    if not player:
        raise GameError('This player session is not valid for this room.', 401)
    touch(room, player)
    if action == 'pick':
        submit_pick(room, player, data.get('foot'), data.get('roundId'))
        return room_snapshot(room, player['token'])
    if action not in ('start', 'next', 'rematch', 'swap', 'lobby'):
        raise GameError('Unknown API route.', 404)
    if player['id'] != room['hostId']:
        raise GameError('Only the host can change the whole-room game state.', 403)
    if action == 'start':
        if room['status'] != 'lobby':
            raise GameError('The game can only start from the lobby.', 409)
        start_game(room)
    elif action == 'next':
        if room['status'] != 'round_complete':
            raise GameError('The next round is available after the current result is complete.', 409)
        begin_turn(room, active_player(room, room['attacker']), active_player(room, room['defender']))
    elif action == 'rematch':
        if room['status'] != 'finished':
            raise GameError('A rematch is available after the game ends.', 409)
        reset_to_lobby(room)
        start_game(room)
    elif action == 'swap':
        if room['status'] != 'finished':
            raise GameError('Sides can only be swapped after a game ends.', 409)
        for p in room['players']:
            p['team'] = 'B' if p['team'] == 'A' else 'A'
        for team in ('A', 'B'):
            for pos, member in enumerate(p for p in room['players'] if p['team'] == team):
                member['position'] = pos
        reset_to_lobby(room)
    else:
        if room['status'] != 'finished':
            raise GameError('Return to lobby is available after a game ends.', 409)
        reset_to_lobby(room)
    return room_snapshot(room, player['token'])

class Handler(BaseHTTPRequestHandler):
    server_version = 'AMPE/1.0'
    def log_message(self, *_):
        pass
    def send_json(self, payload, status=200):
        body = json.dumps(payload, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
    def read_json(self):
        length = self.headers.get('Content-Length')
        if length is None:
            raise GameError('A request body is required.', 400)
        try:
            size = int(length)
        except ValueError:
            raise GameError('Invalid content length.', 400)
        if size < 0 or size > MAX_BODY:
            raise GameError('Request body is too large.', 413)
        return parse_json_body(self.rfile.read(size))
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/api/health':
            return self.send_json({'ok': True, 'service': 'ampe-authoritative'})
        if path.startswith('/api/state/'):
            code = path[len('/api/state/'):].upper()
            if not code or '/' in code:
                return self.send_json({'error': 'Room not found.'}, 404)
            with LOCK:
                prune_rooms()
                room = ROOMS.get(code)
                if not room:
                    return self.send_json({'error': 'This room no longer exists. Ask the host for a new room link.'}, 404)
                try:
                    data = room_snapshot(room, self.headers.get('X-Player-Token', ''))
                except GameError as e:
                    return self.send_json({'error': str(e)}, e.status)
                return self.send_json(data)
        if path in ('/', '/index.html'):
            data = (ROOT / 'index.html').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(data)
            return
        return self.send_json({'error': 'Not found.'}, 404)
    def do_POST(self):
        path = urlsplit(self.path).path
        try:
            data = self.read_json()
            with LOCK:
                prune_rooms()
                if path == '/api/create':
                    return self.send_json(create_session(data), 201)
                if path == '/api/join':
                    return self.send_json(join_session(data), 201)
                parts = path.strip('/').split('/')
                if len(parts) != 3 or parts[0] != 'api' or not parts[2]:
                    raise GameError('Unknown API route.', 404)
                action, code = parts[1], parts[2].upper()
                return self.send_json(player_action(code, action, data))
        except GameError as e:
            return self.send_json({'error': str(e)}, e.status)
        except Exception:
            return self.send_json({'error': 'The server could not process that request.'}, 500)

def add_player(room, name, team):
    return make_player(room, name, team)

if __name__ == '__main__':
    host = os.environ.get('HOST', '0.0.0.0')
    port = int(os.environ.get('PORT', '8000'))
    print(f'Ampe server listening on {host}:{port}', flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
