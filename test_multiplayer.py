import time
import unittest
from pathlib import Path
import server


def reset():
    with server.LOCK:
        server.ROOMS.clear()

def create(mode='duel', size=2, target=10, name='Host'):
    with server.LOCK:
        return server.create_session({'mode':mode,'teamSize':size,'targetScore':target,'name':name})

def join(code, name):
    with server.LOCK:
        return server.join_session({'code':code,'name':name})

def action(code, kind, token, **kwargs):
    with server.LOCK:
        return server.player_action(code,kind,{'token':token,**kwargs})

def snapshot(code, token):
    with server.LOCK:
        return server.room_snapshot(server.ROOMS[code],token)

def begin(room, a, d, seconds=60):
    room['status']='choosing'; room['prepCue']=False; room['attacker']=a['id']; room['defender']=d['id']
    room['round']+=1; room['roundId']=f"round-{room['round']}"; room['settledRoundId']=None
    room['deadlineMono']=time.monotonic()+seconds; room['deadlineEpochMs']=int(time.time()*1000+seconds*1000); room['picks']={}

def play(room, attackerFoot, defenderFoot):
    if room['status']=='round_complete':
        host=server.active_player(room,room['hostId'])
        server.player_action(room['code'],'next',{'token':host['token']})
        server.begin_selection(room['code'],room['roundId'])
        server.start_choosing(room['code'],room['roundId'])
    elif room['status']=='preparing':
        server.begin_selection(room['code'],room['roundId'])
        server.start_choosing(room['code'],room['roundId'])
    a=server.active_player(room,room['attacker']); d=server.active_player(room,room['defender'])
    room['picks']={a['id']:{'foot':attackerFoot,'source':'manual'},d['id']:{'foot':defenderFoot,'source':'manual'}}
    resolved=server.resolve_round(room)
    if resolved and room['status']=='revealing': server.finish_reveal(room['code'],room['roundId'])
    return resolved

class AmpeRules(unittest.TestCase):
    def setUp(self): reset()
    def team_room(self, size=2):
        host=create('teams',size,name='S1'); code=host['state']['code']; players=[host]
        for i in range(size*2-1): players.append(join(code,f'P{i+1}'))
        room=server.ROOMS[code]
        return host,players,room
    def duel_room(self,target=10):
        host=create('duel',target=target,name='Ama'); guest=join(host['state']['code'],'Kofi')
        room=server.ROOMS[host['state']['code']]; server.start_game(room)
        server.begin_selection(room['code'],room['roundId']); server.start_choosing(room['code'],room['roundId'])
        return host,guest,room

    def test_game_start_uses_three_second_prep_cue_then_five_second_choice(self):
        host=create(); guest=join(host['roomCode'],'Kofi'); room=server.ROOMS[host['roomCode']]
        self.assertEqual(room['status'],'lobby')
        with self.assertRaises(server.GameError): server.player_action(room['code'],'start',{'token':guest['token']})
        started=server.player_action(room['code'],'start',{'token':host['token']})
        self.assertEqual(started['status'],'preparing'); self.assertGreaterEqual(started['remainingMs'],server.PREPARE_SECONDS*1000-150)
        rid=room['roundId']; server.begin_selection(room['code'],rid)
        cue=snapshot(room['code'],host['token']); self.assertEqual(cue['status'],'preparing'); self.assertTrue(cue['prepCue'])
        self.assertLessEqual(cue['remainingMs'],int(server.PREP_CUE_SECONDS*1000)); self.assertGreater(cue['remainingMs'],0)
        server.start_choosing(room['code'],rid)
        selecting=snapshot(room['code'],host['token']); self.assertEqual(selecting['status'],'choosing')
        self.assertGreaterEqual(selecting['remainingMs'],server.PICK_SECONDS*1000-150)

    def test_round_result_pauses_until_host_starts_next(self):
        host,guest,room=self.duel_room(); a,b=room['players']; rid=room['roundId']
        server.submit_pick(room,a,'LEFT',rid); server.submit_pick(room,b,'LEFT',rid)
        self.assertEqual(room['status'],'revealing'); self.assertEqual(room['round'],1)
        server.finish_reveal(room['code'],rid)
        self.assertEqual(room['status'],'round_complete'); score=[p['score'] for p in room['players']]
        time.sleep(.03)
        self.assertEqual((room['round'],room['status']), (1,'round_complete')); self.assertEqual([p['score'] for p in room['players']],score)
        with self.assertRaises(server.GameError): action(room['code'],'next',guest['token'])
        before=(room['attacker'],room['defender'],room['round'])
        next_state=action(room['code'],'next',host['token'])
        self.assertEqual(next_state['status'],'preparing'); self.assertEqual(room['round'],before[2]+1)
        self.assertEqual((room['attacker'],room['defender']),before[:2])
        self.assertEqual([p['score'] for p in room['players']],score)

    def test_timeout_resolves_exactly_one_exchange_and_stays_paused(self):
        host,guest,room=self.duel_room(); old=room['roundId']; score=[p['score'] for p in room['players']]
        room['deadlineMono']=time.monotonic()-1
        server.resolve_timeout(room['code'],old)
        self.assertEqual(room['status'],'revealing'); server.finish_reveal(room['code'],old)
        self.assertEqual((room['status'],room['round'],room['roundId']),('round_complete',1,old))
        resolved=room['lastResult'].copy(); server.resolve_timeout(room['code'],old)
        self.assertEqual((room['round'],room['lastResult']), (1,resolved))
        self.assertNotEqual([p['score'] for p in room['players']],score)

    def test_next_round_preserves_team_eliminations_and_rotation(self):
        host,players,room=self.team_room(3); a=[p for p in room['players'] if p['team']=='A']; b=[p for p in room['players'] if p['team']=='B']
        begin(room,a[0],b[0]); play(room,'LEFT','LEFT')
        self.assertEqual(room['status'],'round_complete'); self.assertFalse(b[0]['alive'])
        expected=(a[0]['id'],b[1]['id']); scores=[p['score'] for p in room['players']]
        state=action(room['code'],'next',host['token'])
        self.assertEqual(state['status'],'preparing'); self.assertEqual((room['attacker'],room['defender']),expected)
        self.assertFalse(b[0]['alive']); self.assertEqual([p['score'] for p in room['players']],scores)

    def test_session_snapshot_shows_only_own_locked_foot(self):
        host,guest,room=self.duel_room(); a,b=room['players']; rid=room['roundId']
        server.submit_pick(room,a,'RIGHT',rid)
        sa=snapshot(room['code'],host['token']); sb=snapshot(room['code'],guest['token'])
        self.assertEqual(sa['yourFoot'],'RIGHT'); self.assertIsNone(sb['yourFoot'])
        self.assertNotIn('RIGHT',str(sb))

    def test_all_four_foot_combinations_use_team_identity_even_when_switch_attacks(self):
        cases=[('LEFT','LEFT','SYNC','A'),('RIGHT','RIGHT','SYNC','A'),('LEFT','RIGHT','SWITCH','B'),('RIGHT','LEFT','SWITCH','B')]
        for n,(afoot,bfoot,outcome,winner_team) in enumerate(cases):
            for attacker_team in ('A','B'):
                with self.subTest(feet=(afoot,bfoot),attacker=attacker_team):
                    host,players,room=self.team_room(2); a,b=([p for p in room['players'] if p['team']=='A'],[p for p in room['players'] if p['team']=='B'])
                    attacker,defender=(a[0],b[0]) if attacker_team=='A' else (b[0],a[0])
                    begin(room,attacker,defender)
                    room['picks']={attacker['id']:{'foot':afoot,'source':'manual'},defender['id']:{'foot':bfoot,'source':'manual'}}
                    server.resolve_round(room)
                    self.assertEqual(room['lastResult']['outcome'],outcome)
                    self.assertEqual(room['lastResult']['winnerTeam'],winner_team)
                    self.assertEqual(server.active_player(room,room['lastResult']['winner'])['team'],winner_team)
    def test_sync_wins_as_attacker_and_defender(self):
        host,players,room=self.team_room(2); a,b=([p for p in room['players'] if p['team']=='A'],[p for p in room['players'] if p['team']=='B'])
        for attacker,defender in ((a[0],b[0]),(b[0],a[0])):
            begin(room,attacker,defender); play(room,'LEFT','LEFT')
            self.assertEqual(room['lastResult']['winner'],a[0]['id'])
    def test_switch_wins_as_attacker_and_defender(self):
        host,players,room=self.team_room(2); a,b=([p for p in room['players'] if p['team']=='A'],[p for p in room['players'] if p['team']=='B'])
        for attacker,defender in ((a[0],b[0]),(b[0],a[0])):
            begin(room,attacker,defender); play(room,'LEFT','RIGHT')
            self.assertEqual(room['lastResult']['winner'],b[0]['id'])
    def test_consecutive_sync_wins_preserve_same_duel_opponents(self): self.check_duel(('SYNC','SYNC','SYNC'))
    def test_consecutive_switch_wins_preserve_same_duel_opponents(self): self.check_duel(('SWITCH','SWITCH','SWITCH'))
    def test_alternating_results_preserve_same_duel_opponents(self): self.check_duel(('SYNC','SWITCH','SYNC','SWITCH'))
    def check_duel(self, sequence):
        host,guest,room=self.duel_room(15); ids={p['id'] for p in room['players']}; begin(room,*room['players'])
        for result in sequence:
            self.assertEqual({room['attacker'],room['defender']},ids); self.assertNotEqual(room['attacker'],room['defender'])
            play(room,'LEFT','LEFT' if result=='SYNC' else 'RIGHT')
            self.assertEqual(room['lastResult']['outcome'],result)
            self.assertEqual({room['attacker'],room['defender']},ids); self.assertNotEqual(room['attacker'],room['defender'])
    def test_supported_score_targets_end_at_exact_target(self):
        for target in server.SCORE_OPTIONS:
            host,guest,room=self.duel_room(target); a,b=room['players']; a['score']=target-1; begin(room,a,b); play(room,'LEFT','LEFT')
            self.assertEqual(a['score'],target); self.assertEqual(room['status'],'finished'); self.assertEqual(room['winner'],a['id'])
            with self.assertRaises(server.GameError): server.submit_pick(room,a,'LEFT',room['roundId'])
    def test_invalid_target_options_are_rejected(self):
        for value in (0,3,20,'10.0',True):
            with self.assertRaises(server.GameError): server.create_session({'mode':'duel','targetScore':value})
    def test_timeout_randomizes_missing_feet_and_records_sources(self):
        host,guest,room=self.duel_room(); begin(room,*room['players']); server.resolve_round(room,timeout=True)
        result=room['lastResult']; self.assertTrue(result['timeout'])
        self.assertIn(result['attackerFoot'],('LEFT','RIGHT')); self.assertIn(result['defenderFoot'],('LEFT','RIGHT'))
        self.assertEqual((result['attackerSource'],result['defenderSource']),('automatic','automatic'))
    def test_manual_and_automatic_timeout_mix_is_recorded(self):
        host,guest,room=self.duel_room(); a,b=room['players']; begin(room,a,b); rid=room['roundId']; server.submit_pick(room,a,'LEFT',rid); server.resolve_round(room,timeout=True)
        self.assertEqual(room['lastResult']['attackerSource'],'manual'); self.assertEqual(room['lastResult']['defenderSource'],'automatic')
    def test_five_second_server_timer_resolves_without_player_submissions(self):
        host,guest,room=self.duel_room()
        time.sleep(server.PICK_SECONDS+0.2)
        self.assertIsNotNone(room['lastResult'])
        self.assertTrue(room['lastResult']['timeout'])
        self.assertEqual((room['lastResult']['attackerSource'],room['lastResult']['defenderSource']),('automatic','automatic'))
    def test_late_pick_is_rejected_and_timeout_resolves_once(self):
        host,guest,room=self.duel_room(); a,b=room['players']; begin(room,a,b,seconds=-1); old=room['roundId']
        with self.assertRaises(server.GameError) as caught: server.submit_pick(room,a,'LEFT',old)
        self.assertEqual(caught.exception.status,409); self.assertTrue(room['lastResult']['timeout']); self.assertEqual(room['roundId'],old); self.assertEqual(room['status'],'revealing')
    def test_duplicate_pick_rejected_and_does_not_replace_first_foot(self):
        host,guest,room=self.duel_room(); a,b=room['players']; begin(room,a,b); rid=room['roundId']
        server.submit_pick(room,a,'LEFT',rid)
        with self.assertRaises(server.GameError): server.submit_pick(room,a,'RIGHT',rid)
        self.assertEqual(room['picks'][a['id']]['foot'],'LEFT')
    def test_stale_round_submission_cannot_affect_later_round(self):
        host,guest,room=self.duel_room(); a,b=room['players']; begin(room,a,b); old=room['roundId']; begin(room,b,a)
        with self.assertRaises(server.GameError): server.submit_pick(room,b,'LEFT',old)
        self.assertFalse(room['picks'])
    def test_two_manual_choices_resolve_early_and_never_twice(self):
        host,guest,room=self.duel_room(); a,b=room['players']; begin(room,a,b); rid=room['roundId']
        server.submit_pick(room,a,'LEFT',rid); self.assertEqual(room['status'],'choosing')
        server.submit_pick(room,b,'RIGHT',rid); self.assertEqual(room['lastResult']['outcome'],'SWITCH'); self.assertEqual(room['status'],'revealing')
        server.finish_reveal(room['code'],rid); version=room['stateVersion']; score=[p['score'] for p in room['players']]
        server.resolve_timeout(room['code'],rid)
        self.assertEqual((room['roundId'],room['status']),(rid,'round_complete')); self.assertEqual([p['score'] for p in room['players']],score)
        state=action(room['code'],'next',host['token']); self.assertEqual(state['status'],'preparing'); self.assertGreater(room['stateVersion'],version)
        with self.assertRaises(server.GameError): action(room['code'],'next',host['token'])
    def test_team_lineups_remain_fixed_across_attacker_reversals(self):
        host,players,room=self.team_room(3); a=[p for p in room['players'] if p['team']=='A']; b=[p for p in room['players'] if p['team']=='B']
        begin(room,a[0],b[0]); play(room,'LEFT','LEFT'); self.assertFalse(b[0]['alive']); self.assertEqual((room['attacker'],room['defender']),(a[0]['id'],b[1]['id']))
        play(room,'LEFT','RIGHT'); self.assertFalse(a[0]['alive']); self.assertEqual((room['attacker'],room['defender']),(b[1]['id'],a[1]['id']))
        play(room,'LEFT','LEFT'); self.assertFalse(b[1]['alive']); self.assertEqual((room['attacker'],room['defender']),(a[1]['id'],b[2]['id']))
    def test_eliminated_player_cannot_become_active_again(self):
        host,players,room=self.team_room(2); a=[p for p in room['players'] if p['team']=='A']; b=[p for p in room['players'] if p['team']=='B']; b[0]['alive']=False
        with self.assertRaises(server.GameError): server.begin_turn(room,a[0],b[0])
        self.assertIs(server.next_alive(room,'B'),b[1])
    def test_last_elimination_declares_winning_team(self):
        host,players,room=self.team_room(2); a=[p for p in room['players'] if p['team']=='A']; b=[p for p in room['players'] if p['team']=='B']; attacker,defender=a[0],b[0]
        for turn in range(2):
            begin(room,attacker,defender); play(room,'LEFT','RIGHT')
            if turn==0: attacker,defender=server.active_player(room,room['attacker']),server.active_player(room,room['defender'])
        self.assertEqual((room['status'],room['winner']),('finished','B')); self.assertIn('Team B wins',room['lastResult']['message'])
        self.assertFalse(any(p['alive'] for p in a))
    def test_create_join_assigns_unique_ids_and_balanced_sides(self):
        for size in server.TEAM_OPTIONS:
            host=create('teams',size); code=host['roomCode']; members=[host]
            self.assertEqual(code,host['state']['code'])
            for n in range(size*2-1): members.append(join(code,f'P{n}'))
            room=server.ROOMS[code]; counts={t:sum(p['team']==t for p in room['players']) for t in ('A','B')}
            self.assertEqual(counts,{'A':size,'B':size}); self.assertEqual(len({p['id'] for p in room['players']}),2*size)
            self.assertEqual(len({p['position'] for p in room['players'] if p['team']=='A'}),size)
            with self.assertRaises(server.GameError): join(code,'Too many')
    def test_game_cannot_start_until_both_lineups_are_full(self):
        host=create('teams',2); join(host['state']['code'],'Guest')
        with self.assertRaises(server.GameError): server.start_game(server.ROOMS[host['state']['code']])
        host,players,room=self.team_room(2); server.start_game(room); self.assertEqual(room['status'],'preparing')
    def test_snapshots_restore_sessions_without_exposing_tokens_or_private_feet(self):
        host,guest,room=self.duel_room(); state_a=snapshot(room['code'],host['token']); state_b=snapshot(room['code'],guest['token'])
        self.assertEqual(state_a['you'],host['state']['you']); self.assertEqual(state_b['you'],guest['state']['you'])
        self.assertNotIn('token',str(state_a)); self.assertNotIn('picks',state_a); self.assertNotIn('picks',state_b)
        with self.assertRaises(server.GameError): snapshot(room['code'],'invalid')
    def test_two_player_sessions_receive_identical_authoritative_results(self):
        host,guest,room=self.duel_room(); a,b=room['players']; rid=room['roundId']
        server.submit_pick(room,a,'LEFT',rid); server.submit_pick(room,b,'LEFT',rid)
        sa=snapshot(room['code'],host['token']); sb=snapshot(room['code'],guest['token'])
        self.assertEqual(sa['lastResult'],sb['lastResult']); self.assertEqual(sa['players'],sb['players'])
    def test_rematch_resets_state_and_preserves_selected_target(self):
        host,guest,room=self.duel_room(5); a,b=room['players']
        for _ in range(5): begin(room,a,b); play(room,'LEFT','LEFT')
        self.assertEqual(room['status'],'finished')
        with self.assertRaises(server.GameError): server.player_action(room['code'],'rematch',{'token':guest['token']})
        again=action(room['code'],'rematch',host['token'])
        self.assertEqual((again['status'],again['targetScore']),('preparing',5)); self.assertTrue(all(p['score']==0 for p in again['players']))
    def test_swap_sides_is_host_only_and_preserves_team_sizes_and_positions(self):
        host,players,room=self.team_room(2); a=[p for p in room['players'] if p['team']=='A']; b=[p for p in room['players'] if p['team']=='B']
        server.start_game(room)
        while room['status']!='finished': play(room,'LEFT','RIGHT')
        before={p['id']:p['team'] for p in room['players']}
        with self.assertRaises(server.GameError): server.player_action(room['code'],'swap',{'token':players[1]['token']})
        state=action(room['code'],'swap',host['token']); after={p['id']:p['team'] for p in room['players']}
        self.assertTrue(all(after[k]!=v for k,v in before.items())); self.assertEqual(state['status'],'lobby')
        self.assertEqual(sum(p['team']=='A' for p in room['players']),2); self.assertEqual(sorted(p['position'] for p in room['players'] if p['team']=='B'),[0,1])
    def test_return_to_lobby_resets_completed_match(self):
        host,guest,room=self.duel_room(5); a,b=room['players']
        for _ in range(5): begin(room,a,b); play(room,'LEFT','LEFT')
        lobby=action(room['code'],'lobby',host['token'])
        self.assertEqual(lobby['status'],'lobby'); self.assertTrue(all(p['score']==0 and p['alive'] for p in lobby['players']))
    def test_host_disconnect_transfers_host_after_timeout(self):
        host,guest,room=self.duel_room(); room['players'][0]['lastSeen']=time.time()-40
        restored=snapshot(room['code'],guest['token'])
        self.assertTrue(restored['isHost']); self.assertEqual(room['hostId'],guest['state']['you'])
    def test_malformed_oversized_and_non_object_json_are_rejected(self):
        for body,status in ((b'{oops',400),(b' '*(server.MAX_BODY+1),413),(b'[]',400)):
            with self.assertRaises(server.GameError) as caught: server.parse_json_body(body)
            self.assertEqual(caught.exception.status,status)
    def test_inactive_room_pruning_does_not_touch_recent_rooms(self):
        host,guest,room=self.duel_room(); room['lastActivity']=time.time()-server.ROOM_TTL-1
        server.prune_rooms(); self.assertNotIn(room['code'],server.ROOMS)
    def test_invite_url_deep_link_and_mobile_animation_hooks_are_present(self):
        html=(Path(__file__).parent/'index.html').read_text(encoding='utf-8')
        self.assertIn('/?room=${encodeURIComponent(gameCode)}',html); self.assertIn("new URLSearchParams(location.search).get('room')",html)
        self.assertIn('@keyframes jump',html); self.assertIn('@media(max-width:420px)',html)

if __name__=='__main__': unittest.main(verbosity=2)

