"""Synthetic evidence capability checks; no owner logs, services or models are used."""
import copy
import json
import os
import shutil
import subprocess
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from deck_log import DeckLog
from officer_evidence import EvidenceService, _interval


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.now=1800000000.0
        self.path=os.path.join(os.path.realpath(self.tmp.name),'state')
        self.archive=DeckLog(self.path,clock=lambda:self.now,limits={'fts':False,'bytes':256*1024*1024})
        self.epoch=self.archive.configure(enabled=True)['capture_epoch']
        self.archive.activate(self.epoch)
        self.number=0
        self.live=dict(boot='fictional-boot',cutoff_seq=10,floor_seq=1,subjects=[dict(chat_id='x:live',chat_name='Atlas',provider='codex',project_id='a'*64,project_name='Orchard')],
                       projects=[],facts=[dict(subject='x:live',field='model',value='fictional-model',source='WATCH observation',source_ts=self.now,observed_ts=self.now,qualification='Last recorded selection',freshness='unknown')],events=[])
        self.service=EvidenceService(self.archive,lambda:self.live,max_work=1000000)

    def tearDown(self):
        self.archive.close()
        self.tmp.cleanup()

    def seed(self,n=1,chat='x:archived',text='A useful update',offset=0,**extra):
        records=[];rows=[]
        for i in range(n):
            self.number+=1
            record=dict(key=self.archive.opaque_identity('record',self.number),stream=self.archive.opaque_identity('stream','fictional'),provider='codex',chat_id=chat,project_id='a'*64,source_ts=self.now+offset+i,time_basis='source')
            candidate=dict(kind='message',role='assistant',text=text,chat_name='Atlas',project_name='Orchard',source={'slot':'0'},**extra)
            _,prepared,observed=self.archive._prepare(record,[candidate])
            records.append((record['key'],record['stream'],chat,record['project_id'],record['source_ts'],observed,min(record['source_ts'],observed)))
            rows.extend(prepared)
        with self.archive.lock:
            self.archive.db.executemany('INSERT INTO source_records VALUES (?,?,?,?,?,?,?)',records)
            self.archive.db.executemany('INSERT INTO events VALUES ('+','.join('?'*21)+')',rows)
            self.archive.db.executemany('INSERT INTO event_ingest(event_id) VALUES (?)',[(r[0],) for r in rows])
            self.archive.db.commit()
        return [r[0] for r in rows]

    def query(self,session,**kwargs):
        return session.tool('evidence_query',dict(scope={'sources':['retained']},**kwargs))

    def test_monotonic_ingest_survives_reopen_delete_and_late_source_dates(self):
        self.seed()
        old=self.archive.evidence_state()
        self.archive.close();self.archive=DeckLog(self.path,clock=lambda:self.now)
        self.assertTrue(self.archive.available)
        self.assertEqual(self.archive.evidence_state()['fence'],old['fence'])
        self.service.history=self.archive
        session=self.service.start()
        self.seed(offset=-86400)
        result=self.query(session)
        self.assertEqual(result['population']['counts']['retained']['events'],1)
        before=self.archive.evidence_state()
        self.archive.delete(all=True)
        erased=self.archive.evidence_state()
        self.assertGreater(erased['erasure'],before['erasure'])
        self.seed(text='New evidence',offset=1)
        self.assertGreater(self.archive.evidence_state()['fence'],before['fence'])
        self.assertTrue(session.validate()['valid'])
        self.assertFalse(session.validate()['needs_fallback'])

    def test_legacy_migration_backfills_without_changing_ids(self):
        ids=self.seed(3)
        with self.archive.lock:
            self.archive.db.execute('DROP TABLE event_ingest')
            self.archive.db.execute("UPDATE settings SET value='2' WHERE key='schema_version'")
            self.archive.db.commit()
            self.archive._schema()
        self.assertEqual({e['id'] for e in self.archive.search()['events']},set(ids))
        self.assertEqual(self.archive.evidence_state()['fence'],3)

    def test_scope_first_search_and_centered_matching_excerpt(self):
        self.seed(chat='x:quiet',text='z'*1100+' NEEDLE '+'z'*600)
        self.seed(1200,chat='x:busy',offset=1,text='No relevant evidence')
        session=self.service.start()
        result=session.tool('evidence_query',dict(scope={'sources':['retained'],'chat_ids':['x:quiet']},match={'text':'NEEDLE','mode':'literal'}))
        self.assertEqual(result['population']['counts']['retained']['events'],1)
        self.assertIn('NEEDLE',result['evidence'][0]['text'])
        self.assertNotIn('z'*1000,result['evidence'][0]['text'])

    def test_full_50000_counts_are_independent_of_supplied_sample(self):
        self.seed(50000,text='A recorded update')
        started=time.monotonic()
        result=self.query(self.service.start(),view='aggregates')
        elapsed=time.monotonic()-started
        self.assertNotIn('error',result)
        self.assertEqual(result['population']['counts']['retained']['events'],50000)
        self.assertEqual(result['evidence'],[])
        self.assertTrue(result['population']['complete'])
        self.assertLess(elapsed,10.0)
        quantity=next(q for q in result['quantities'] if q['metric']=='events')
        self.assertEqual(quantity['unit'],'recorded_events')
        self.assertEqual(quantity['value'],50000)

    def test_cursor_is_bound_to_scope_order_and_actual_supplied_records(self):
        ids=self.seed(30,text='x'*1900)
        service=EvidenceService(self.archive,lambda:self.live,max_bytes=8000,max_work=1000000)
        session=service.start();seen=[];cursor=None
        for _ in range(40):
            page=self.query(session,limit=30,**({'cursor':cursor} if cursor else {}))
            self.assertLessEqual(len(json.dumps(page).encode()),8000)
            seen.extend(e['ref'][5:] for e in page['evidence'])
            cursor=page['continuation']['cursor']
            if not cursor:break
        self.assertEqual(set(seen),set(ids));self.assertEqual(len(seen),len(ids))
        first=self.query(session,limit=1)
        invalid=self.query(session,limit=1,order='desc',cursor=first['continuation']['cursor'])
        self.assertIn('error',invalid)
        self.archive.delete(chat_id='x:archived')
        removed=self.query(session,limit=1,cursor=first['continuation']['cursor'])
        self.assertEqual(removed['error'],'invalid_or_changed_cursor')

    def test_overview_represents_quiet_and_busy_and_exposes_middle(self):
        self.seed(80,chat='x:busy',text='busy',offset=0)
        self.seed(chat='x:quiet',text='quiet decision',offset=40)
        result=self.query(self.service.start(),view='overview',limit=6)
        self.assertEqual({e['chat_id'] for e in result['evidence']},{'x:busy','x:quiet'})
        busy=[e for e in result['evidence'] if e['chat_id']=='x:busy']
        self.assertTrue(any(30<=e['source_ts']-self.now<=50 for e in busy))
        self.assertIn('semantic importance is not guaranteed',result['selection']['method'])
        self.assertGreater(result['selection']['omitted_detail'],0)

    def test_overview_does_not_bury_sparse_prose_under_operations(self):
        self.seed(1000,text='',chat='x:busy',operation='read',phase='started',outcome='pending',offset=0)
        # Preserve existing capture shape: operations are structural, not prose claims.
        with self.archive.lock:
            self.archive.db.execute("UPDATE events SET kind='operation',role='' WHERE chat_id='x:busy'")
            self.archive.db.commit()
        self.seed(chat='x:busy',text='The first approach was rejected',offset=300)
        self.seed(chat='x:busy',text='A middle-period decision changed the approach',offset=500)
        self.seed(chat='x:busy',text='The revised approach was reported',offset=700)
        self.seed(chat='x:quiet',text='A quiet milestone was reported',offset=400)
        result=self.query(self.service.start(),view='overview',limit=4)
        prose=[e['text'] for e in result['evidence'] if e['kind']=='message']
        self.assertTrue(any('middle-period' in t for t in prose))
        self.assertTrue(any('quiet milestone' in t for t in prose))
        self.assertGreater(result['selection']['omitted_detail'],900)

    def test_context_and_cross_day_operation_pair_keep_each_certainty(self):
        op='b'*64
        starts=self.seed(text='',offset=-86400,operation='edit',operation_id=op,phase='started',outcome='pending',certainty='probably')
        self.seed(text='Decision was revised',offset=-10)
        results=self.seed(text='',operation='tool',operation_id=op,phase='result',outcome='success')
        session=self.service.start()
        expanded=session.tool('evidence_expand',dict(refs=['deck:'+starts[0]],mode='operation'))
        self.assertEqual({e['ref'][5:] for e in expanded['evidence']},set(starts+results))
        self.assertEqual({e['certainty'] for e in expanded['evidence']},{'probably','recorded'})
        self.assertTrue(expanded['relationships'][0]['result_observed'])
        context=session.tool('evidence_expand',dict(refs=['deck:'+results[0]],mode='context',before=1,after=0))
        self.assertTrue(any('revised' in e['text'] for e in context['evidence']))

    def test_current_facts_are_typed_and_followup_fingerprints_change_with_value(self):
        session=self.service.start()
        catalog=session.tool('room_snapshot',dict(fields=['model']))
        fact=catalog['facts'][0]
        session.mark_supplied(catalog)
        self.assertTrue(session.validate()['valid'])
        self.live['facts'][0]['observed_ts']+=100
        self.assertTrue(session.validate()['valid'])
        self.live['facts'][0]['value']='different-model'
        check=session.validate()
        self.assertTrue(check['valid'])
        self.assertEqual(check['assessment']['refs'][fact['ref']]['status'],'stale')
        self.assertFalse(check['needs_fallback'])
        fresh=self.service.start().resolve(fact['ref'])
        self.assertNotEqual(fresh['fingerprint'],fact['fingerprint'])
        receipt=session.receipt()
        self.assertIn(fact['ref'],receipt['reference_fingerprints'])
        self.assertNotIn('different-model',json.dumps(receipt))

    def test_supplied_refs_only_and_live_window_changes_qualify(self):
        self.live['events']=[dict(seq=1,chat_id='x:live',provider='codex',source_ts=None,observed_ts=self.now,kind='message',role='assistant',text='A recorded statement',certainty='recorded')]
        session=self.service.start()
        result=session.tool('evidence_query',dict(scope={'sources':['live']},interval={'since':self.now-1,'until':self.now+1}))
        self.assertEqual(result['population']['counts']['live']['events'],1)
        self.assertEqual(session.receipt()['refs'],[])
        session.mark_supplied(result)
        self.assertTrue(session.validate()['valid'])
        self.live['events']=[];self.live['floor_seq']=2
        check=session.validate()
        self.assertTrue(check['valid'])
        self.assertIn('live_observation_unavailable',check['warnings'])

    def test_catalog_keeps_duplicate_names_worktrees_and_pages(self):
        self.seed(chat='x:one');self.seed(chat='x:two')
        session=self.service.start()
        first=session.tool('room_snapshot',dict(limit=1))
        second=session.tool('room_snapshot',dict(limit=1,cursor=first['continuation']['cursor']))
        self.assertNotEqual(first['subjects'][0]['chat_id'],second['subjects'][0]['chat_id'])
        self.assertEqual(first['subjects'][0]['chat_name'],second['subjects'][0]['chat_name'])
        self.assertTrue(first['population']['complete'])

    def test_retained_only_and_old_project_catalog_never_supply_live_facts(self):
        self.seed(chat='x:live')
        session=self.service.start()
        retained=session.tool('room_snapshot',dict(scope={'sources':['retained']},fields=['model']))
        self.assertEqual(retained['facts'],[])
        self.live['subjects'][0]['project_id']='b'*64
        changed=self.service.start()
        old=changed.tool('room_snapshot',dict(scope={'project_ids':['a'*64]},fields=['model']))
        self.assertEqual(old['facts'],[])
        current=changed.tool('room_snapshot',dict(scope={'sources':['live'],'project_ids':['b'*64]},fields=['model']))
        self.assertEqual(current['facts'][0]['source'],'live')

    def test_work_limit_is_explicit_never_a_false_total(self):
        self.seed(10)
        service=EvidenceService(self.archive,lambda:self.live,max_work=2)
        result=self.query(service.start(),view='aggregates')
        self.assertFalse(result['population']['complete'])
        self.assertIsNone(result['population']['counts']['retained']['events'])
        self.assertTrue(all(q['value'] is None for q in result['quantities']))
        self.assertEqual(result['coverage']['sources'][0]['reason'],'query_work_exhausted')

    def test_no_cross_source_inferred_deduplication(self):
        self.seed(text='Same statement')
        self.live['events']=[dict(seq=1,chat_id='x:archived',source_ts=self.now,observed_ts=self.now,kind='message',role='assistant',text='Same statement',certainty='recorded')]
        result=self.service.start().tool('evidence_query',dict(view='aggregates'))
        self.assertEqual(result['population']['counts']['retained']['events'],1)
        self.assertEqual(result['population']['counts']['live']['events'],1)
        self.assertIn('separately',result['population']['deduplication'])

    def test_group_counts_have_registered_exact_membership_and_filters(self):
        self.seed(4,chat='x:one',outcome='failure')
        self.seed(2,chat='x:two',outcome='failure')
        self.seed(3,chat='x:two',outcome='success')
        result=self.query(self.service.start(),view='aggregates',group_by='chat',filters={'outcome':['failure']})
        quantities={q['ref']:q for q in result['quantities']}
        self.assertEqual(len(result['groups']),2)
        for group in result['groups']:
            q=quantities[group['quantity_ref']]
            self.assertEqual(q['value'],4 if group['key']=='x:one' else 2)
            self.assertEqual(q['scope']['chat_ids'],[group['key']])
            self.assertEqual(q['scope']['filters'],{'outcome':['failure']})
            self.assertEqual(q['scope']['sources'],['retained'])

    def test_text_filtered_quantities_bind_digest_without_recording_raw_query(self):
        self.seed(text='A very particular statement')
        session=self.service.start()
        result=self.query(session,view='aggregates',match={'text':'particular','mode':'literal'})
        for quantity in result['quantities']:
            self.assertEqual(quantity['scope']['match']['mode'],'literal')
            self.assertEqual(len(quantity['scope']['match']['digest']),64)
        session.mark_supplied(result)
        self.assertNotIn('particular',json.dumps(session.receipt()))

    def test_erasure_hook_follows_committed_delete_and_retention(self):
        self.seed()
        generations=[]
        def observer(generation):
            row=self.archive.db.execute("SELECT value FROM settings WHERE key='erasure_generation'").fetchone()
            self.assertEqual(int(row[0]),generation)
            self.assertFalse(self.archive.db.in_transaction)
            generations.append(generation)
        self.archive.erasure_observer=observer
        self.archive.delete(chat_id='x:archived')
        self.assertEqual(len(generations),1)
        self.seed(offset=-40*86400)
        self.archive.evidence_state()
        self.assertEqual(len(generations),2)
        self.assertGreater(generations[1],generations[0])

    def test_calendar_convenience_filters_actual_population_and_normalizes(self):
        self.seed(text='Earlier observation',offset=-86400)
        self.seed(text='Requested-day observation')
        day=time.strftime('%Y-%m-%d',time.localtime(self.now))
        result=self.query(self.service.start(),interval={'day':day})
        self.assertEqual(set(result['interval']),{'since','until','time_basis'})
        self.assertEqual(result['population']['counts']['retained']['events'],1)
        self.assertEqual(result['evidence'][0]['text'],'Requested-day observation')
        week=_interval({'start_day':'2026-10-05','end_day':'2026-10-11'})
        self.assertEqual(time.strftime('%Y-%m-%d %H:%M',time.localtime(week['since'])),'2026-10-05 00:00')
        self.assertEqual(time.strftime('%Y-%m-%d %H:%M',time.localtime(week['until'])),'2026-10-12 00:00')

    @unittest.skipUnless(hasattr(time,'tzset'),'Local DST timezone control unavailable')
    def test_calendar_days_and_weeks_use_dst_local_midnights(self):
        previous=os.environ.get('TZ')
        try:
            os.environ['TZ']='America/New_York';time.tzset()
            for day,hours in [('2026-03-08',23),('2026-11-01',25)]:
                interval=_interval({'day':day})
                self.assertEqual(interval['until']-interval['since'],hours*3600)
                self.assertEqual(time.strftime('%H:%M',time.localtime(interval['since'])),'00:00')
                self.assertEqual(time.strftime('%H:%M',time.localtime(interval['until'])),'00:00')
            week=_interval({'start_day':'2026-03-02','end_day':'2026-03-08'})
            self.assertEqual(week['until']-week['since'],167*3600)
            fall=_interval({'start_day':'2026-10-26','end_day':'2026-11-01'})
            self.assertEqual(fall['until']-fall['since'],169*3600)
        finally:
            if previous is None:os.environ.pop('TZ',None)
            else:os.environ['TZ']=previous
            time.tzset()

    def test_recent_minutes_are_snapshot_relative_and_invalid_mixed_forms_rejected(self):
        with patch('officer_evidence.time.time',return_value=self.now):
            session=self.service.start()
        first=self.query(session,interval={'minutes':30})
        with patch('officer_evidence.time.time',return_value=self.now+600):
            second=self.query(session,interval={'minutes':30})
        self.assertEqual(first['interval'],dict(since=self.now-1800,until=self.now,time_basis='source'))
        self.assertEqual(second['interval'],first['interval'])
        for value in [{'day':'2026-11-10','since':0},{'day':'2026-11-10','minutes':30},
                      {'day':'2026-11-10','start_day':'2026-11-01'}, {'end_day':'2026-11-10'},
                      {'start_day':'2026-11-11','end_day':'2026-11-10'},{'day':'2026-02-30'},{'day':'9999-12-31'},
                      {'minutes':0},{'minutes':525601},{'minutes':True}]:
            self.assertIn('error',self.query(session,interval=value))
        self.assertEqual(_interval({}),dict(since=None,until=None,time_basis='source'))

    def test_catalog_quantities_count_live_subjects_without_any_events(self):
        session=self.service.start()
        result=session.tool('room_snapshot',{})
        live=next(q for q in result['quantities'] if q['metric']=='live_chats')
        self.assertEqual(live['value'],1)
        self.assertEqual(live['unit'],'current_live_chats')
        self.assertEqual(live['scope']['sources'],['live'])
        activity=session.tool('evidence_query',dict(scope={'sources':['live']},view='aggregates'))
        self.assertEqual(activity['population']['counts']['live']['chats'],0)
        all_subjects=next(q for q in result['quantities'] if q['metric']=='subjects')
        self.assertEqual(all_subjects['value'],1)
        self.assertEqual(all_subjects['unit'],'chat_project_identities')

    def test_realistic_39_chat_overview_fits_catalog_plus_two_pages_under_64k(self):
        self.now=time.mktime((2026,11,10,9,0,0,0,0,-1))
        subjects=[];events=[]
        for chat in range(39):
            ident='x:'+str(uuid.UUID(int=chat+1))
            name='Agent %02d'%chat
            subjects.append(dict(chat_id=ident,chat_name=name,provider='codex',project_id='a'*64,project_name='Orchard'))
            for step in range(9):
                text=(('Middle milestone for '+name+'. ') if step==4 else name+' update '+str(step)+'. ')+'Useful recorded progress with an explicit qualification. '*8
                text=text[:380]
                self.seed(chat=ident,text=text,offset=step*1800)
                events.append(dict(seq=len(events)+1,chat_id=ident,chat_name=name,provider='codex',project_id='a'*64,project_name='Orchard',source_ts=None,observed_ts=self.now+step*1800,kind='message',role='assistant',text=text,certainty='recorded'))
        self.live.update(subjects=subjects,events=events,cutoff_seq=len(events),facts=[])
        for source in ('live','retained'):
            with self.subTest(source=source):
                session=self.service.start()
                catalog=session.tool('room_snapshot',dict(scope={'sources':[source]},limit=50))
                size=lambda value:len(json.dumps(value,separators=(',',':'),ensure_ascii=True).encode())
                total=size(catalog);session.mark_supplied(catalog)
                seen=set();milestones=set();first_refs=set();cursor=None
                for page in range(2):
                    result=session.tool('evidence_query',dict(scope={'sources':[source]},view='overview',limit=50,**({'cursor':cursor} if cursor else {})))
                    self.assertNotIn('error',result)
                    total+=size(result)
                    seen.update(r['chat_id'] for r in result['evidence'])
                    milestones.update(r['chat_id'] for r in result['evidence'] if 'Middle milestone' in r['text'])
                    self.assertNotIn('groups',result)
                    self.assertTrue(all('fingerprint' not in r and 'id' not in r for r in result['evidence']))
                    if page==0:
                        self.assertEqual({q['metric'] for q in result['quantities']},{'events','chats'})
                        first_refs={q['ref'] for q in result['quantities']}
                    else:
                        self.assertEqual(result['quantities'],[])
                        self.assertEqual(set(result['population']['quantity_refs']),first_refs)
                        self.assertTrue(result['population']['unchanged'])
                    self.assertIn('semantic importance is not guaranteed',result['selection']['method'])
                    session.mark_supplied(result)
                    cursor=result['continuation']['cursor']
                    if not cursor:break
                self.assertEqual(len(seen),39)
                self.assertEqual(len(milestones),39)
                self.assertLessEqual(total,64000)
                self.assertTrue(session.validate()['valid'])

    def test_continuation_resends_quantities_when_prior_page_was_not_supplied(self):
        self.seed(25)
        session=self.service.start()
        first=self.query(session,view='records',limit=2)
        second=self.query(session,view='records',limit=2,cursor=first['continuation']['cursor'])
        self.assertTrue(second['quantities'])
        self.assertFalse(second['population'].get('unchanged',False))
        self.assertEqual(session.receipt()['refs'],[])

    def test_frontier_tracks_post_fit_supply_only_and_query_compatible_pages(self):
        for chat in range(39):
            self.seed(chat='x:chat-'+str(chat),text='A middle observation. '+'x'*360)
        service=EvidenceService(self.archive,lambda:self.live,max_bytes=8000,max_work=1000000)
        session=service.start()
        args=dict(scope={'sources':['retained']},view='overview',limit=50)
        first=session.tool('evidence_query',args)
        supplied=lambda result:{(r['source'],r['chat_id'],r.get('project_id','')) for r in result['evidence']}
        first_views=supplied(first)
        frontier=first['coverage']['frontier']
        self.assertEqual(frontier['matching_views'],39)
        self.assertEqual(frontier['previously_supplied_views'],0)
        self.assertEqual(frontier['in_this_result_views'],len(first_views))
        self.assertEqual(frontier['unrepresented_views'],39-len(first_views))
        self.assertEqual(first['selection']['omitted_groups'],39-len(first_views))
        self.assertLessEqual(len(frontier['unrepresented_ids']),8)
        self.assertLessEqual(len(json.dumps(first,separators=(',',':')).encode()),8000)
        self.assertEqual(session.receipt()['frontiers'][0]['supplied_views'],0)
        next_args=dict(args,cursor=first['continuation']['cursor'])
        unmarked=session.tool('evidence_query',next_args)
        self.assertEqual(unmarked['coverage']['frontier']['previously_supplied_views'],0)
        session.mark_supplied(first)
        second=session.tool('evidence_query',next_args)
        second_views=supplied(second)
        self.assertEqual(second['coverage']['frontier']['previously_supplied_views'],len(first_views))
        self.assertEqual(second['coverage']['frontier']['unrepresented_views'],39-len(first_views|second_views))
        session.mark_supplied(second)
        self.assertEqual(session.receipt()['frontiers'][0]['supplied_views'],len(first_views|second_views))

    def test_frontier_identity_is_normalized_and_receipt_has_no_raw_match(self):
        self.seed(5,text='A private matching phrase was recorded')
        session=self.service.start()
        first=self.query(session,view='overview',limit=2,match={'text':'private matching phrase'})
        session.mark_supplied(first)
        changed_view=self.query(session,view='records',order='desc',limit=3,match={'text':'PRIVATE MATCHING PHRASE'})
        self.assertEqual(first['coverage']['frontier']['query_ref'],changed_view['coverage']['frontier']['query_ref'])
        self.assertEqual(changed_view['coverage']['frontier']['previously_supplied_views'],1)
        other=self.query(session,view='records',match={'text':'private matching phrase'},filters={'outcome':['failure']})
        self.assertNotEqual(first['coverage']['frontier']['query_ref'],other['coverage']['frontier']['query_ref'])
        receipt=session.receipt()
        self.assertNotIn('private matching phrase',json.dumps(receipt).lower())
        self.assertEqual(len(receipt['frontiers']),2)
        self.assertEqual(len(receipt['frontiers'][0]['population_digest']),64)

    def test_frontier_distinguishes_live_and_retained_views_for_same_chat(self):
        self.seed(chat='x:live')
        self.live['events']=[dict(seq=1,chat_id='x:live',project_id='a'*64,source_ts=self.now,observed_ts=self.now,kind='message',role='assistant',text='An independent live view',certainty='recorded')]
        session=self.service.start()
        result=session.tool('evidence_query',{})
        self.assertEqual(result['coverage']['frontier']['matching_views'],2)
        self.assertEqual(result['coverage']['frontier']['in_this_result_views'],2)
        session.mark_supplied(result)
        self.assertEqual(session.receipt()['frontiers'][0]['supplied_views'],2)
        self.assertIn('not establish semantic completeness',result['coverage']['frontier']['qualification'])

    @unittest.skipUnless(shutil.which('node'),'Node required for actual demo fixture')
    def test_actual_demo_cloud_process_subject_is_discoverable_with_unknown_results(self):
        from officer_capabilities import live_view
        fixture=json.loads(subprocess.check_output(['node',str(Path(__file__).resolve().parent/'fixtures/officer/export_demo.cjs')],timeout=5))
        agents={row['agent']:row for row in fixture['records'] if row['kind']=='agent'}
        events=[row for row in fixture['records'] if row['kind']!='agent']
        cloud=next(aid for aid,row in agents.items() if row.get('name')=='Codex cloud')
        self.assertTrue(cloud.startswith('e:'))
        view=live_view('fictional-demo-boot',agents,events,{},now=fixture['now'])
        session=EvidenceService(None,lambda:view).start()
        catalog=session.tool('room_snapshot',dict(scope={'sources':['live'],'chat_ids':[cloud]},fields=['last_activity']))
        self.assertEqual(catalog['subjects'][0]['chat_id'],cloud)
        self.assertEqual(catalog['subjects'][0]['provider'],'codex_cloud')
        self.assertTrue(catalog['facts'])
        result=session.tool('evidence_query',dict(scope={'sources':['live'],'chat_ids':[cloud]},filters={'provider':['codex_cloud'],'phase':['result']}))
        self.assertEqual(len(result['evidence']),2)
        self.assertTrue(all(row['outcome']=='unknown' for row in result['evidence']))
        self.assertTrue(all('process' in row['observation_source'] for row in result['evidence']))
        self.assertTrue(all('not established' in row['source_qualification'] for row in result['evidence']))
        self.assertTrue(all('cmd' not in row for row in result['evidence']))

    def test_missing_operation_identity_makes_only_distinct_count_unknown(self):
        known=self.seed(text='',operation='edit',operation_id='c'*64,phase='started',outcome='pending')
        missing=self.seed(text='',operation='tool',phase='result',outcome='success')
        with self.archive.lock:
            self.archive.db.execute("UPDATE events SET kind='operation',role='' WHERE id IN (?,?)",(known[0],missing[0]))
            self.archive.db.commit()
        self.live['events']=[dict(seq=1,chat_id='x:live',kind='operation',operation='edit',operation_id='native-known-id',phase='started',outcome='pending',certainty='recorded',source_ts=self.now,observed_ts=self.now),
                             dict(seq=2,chat_id='x:live',kind='operation',operation='tool',operation_id='',phase='result',outcome='success',certainty='recorded',source_ts=self.now,observed_ts=self.now)]
        for source in ('live','retained'):
            with self.subTest(source=source):
                result=self.service.start().tool('evidence_query',dict(scope={'sources':[source]},view='aggregates'))
                self.assertTrue(result['population']['complete'])
                self.assertEqual(result['population']['counts'][source]['events'],2)
                self.assertEqual(result['population']['counts'][source]['operation_results'],1)
                self.assertIsNone(result['population']['counts'][source]['operations'])
                self.assertFalse(result['population']['metric_completeness'][source]['operations'])
                self.assertTrue(result['population']['metric_completeness'][source]['events'])
                quantity=next(q for q in result['quantities'] if q['metric']=='operations')
                self.assertIsNone(quantity['value']);self.assertFalse(quantity['complete'])
                self.assertIn('lack stable identities',result['population']['metric_qualifications'][source]['operations'])
                known_only=self.service.start().tool('evidence_query',dict(scope={'sources':[source]},view='aggregates',filters={'phase':['started']}))
                self.assertEqual(known_only['population']['counts'][source]['operations'],1)
                self.assertTrue(known_only['population']['metric_completeness'][source]['operations'])

    def test_message_only_population_has_true_zero_distinct_operations(self):
        self.seed(text='An agent report, without an operation')
        self.live['events']=[dict(seq=1,chat_id='x:live',kind='message',role='assistant',text='Another agent report',certainty='recorded',source_ts=self.now,observed_ts=self.now)]
        for source in ('live','retained'):
            result=self.service.start().tool('evidence_query',dict(scope={'sources':[source]},view='aggregates'))
            self.assertEqual(result['population']['counts'][source]['operations'],0)
            quantity=next(q for q in result['quantities'] if q['metric']=='operations')
            self.assertEqual(quantity['value'],0);self.assertTrue(quantity['complete'])

    def test_pending_validation_does_not_advance_refs_frontier_or_query_work(self):
        self.seed(5)
        session=self.service.start()
        result=self.query(session,view='overview')
        before=copy.deepcopy(session.receipt());work=session.work
        self.assertTrue(session.validate_pending([result])['valid'])
        self.assertEqual(session.receipt(),before)
        self.assertEqual(session.work,work)
        self.assertEqual(session.receipt()['refs'],[])
        self.assertEqual(session.receipt()['frontiers'][0]['supplied_views'],0)
        session.mark_supplied(result)
        self.assertTrue(session.receipt()['refs'])
        self.assertEqual(session.receipt()['frontiers'][0]['supplied_views'],1)

    def test_pending_deleted_evidence_is_rejected_without_supply_promotion(self):
        self.seed()
        session=self.service.start()
        result=self.query(session)
        self.archive.delete(all=True)
        check=session.validate_pending([result])
        self.assertFalse(check['valid'])
        self.assertEqual(check['reason'],'archive_input_removed')
        self.assertEqual(session.receipt()['refs'],[])
        self.assertEqual(session.receipt()['frontiers'][0]['supplied_views'],0)

    def test_pending_changed_fact_and_restart_are_qualified_before_admission(self):
        session=self.service.start()
        result=session.tool('room_snapshot',dict(fields=['model']))
        self.live['facts'][0]['value']='Changed model'
        check=session.validate_pending([result])
        self.assertTrue(check['valid']);self.assertIn('observation_changed',check['warnings'])
        self.assertEqual(session.original_observation(result['facts'][0]['ref'])['value'],'fictional-model')
        self.assertEqual(session.receipt()['refs'],[])
        self.live['facts'][0]['value']='fictional-model'
        self.live['boot']='another-boot'
        check=session.validate_pending([result])
        self.assertTrue(check['valid']);self.assertIn('live_boot_changed',check['warnings'])

    def test_pending_live_quantities_detect_floor_change_without_record_refs(self):
        self.live['events']=[dict(seq=1,chat_id='x:live',kind='message',role='assistant',text='Retained in live ring',certainty='recorded',source_ts=self.now,observed_ts=self.now)]
        session=self.service.start()
        result=session.tool('evidence_query',dict(scope={'sources':['live']},view='aggregates'))
        self.assertEqual(result['evidence'],[])
        self.live['floor_seq']=2
        check=session.validate_pending([result])
        self.assertTrue(check['valid']);self.assertIn('population_changed',check['warnings'])
        self.assertEqual(session.receipt()['refs'],[])

    def test_pending_live_records_are_fingerprint_checked_with_one_projection(self):
        self.live['events']=[dict(seq=i+1,chat_id='x:live',kind='message',role='assistant',text='Statement '+str(i),certainty='recorded',source_ts=self.now+i,observed_ts=self.now+i) for i in range(20)]
        self.live['cutoff_seq']=20
        calls=[]
        def view():calls.append(1);return self.live
        session=EvidenceService(self.archive,view,max_work=1000000).start()
        result=session.tool('evidence_query',dict(scope={'sources':['live']},limit=20))
        calls.clear();work=session.work
        self.assertTrue(session.validate_pending([result])['valid'])
        self.assertEqual(len(calls),1)
        self.assertEqual(session.work,work)
        self.live['events'][0]['observed_ts']+=1
        check=session.validate_pending([result])
        self.assertTrue(check['valid']);self.assertIn('observation_changed',check['warnings'])
        self.assertEqual(session.receipt()['refs'],[])

    def test_legacy_reference_staging_is_metadata_only_and_detects_changed_prose(self):
        self.live['events']=[dict(seq=1,chat_id='x:live',kind='message',role='assistant',text='Original statement',certainty='recorded',source_ts=self.now,observed_ts=self.now)]
        session=self.service.start();ref='live:'+self.live['boot']+':1';work=session.work
        self.assertTrue(session.prepare_pending_refs([ref])['valid'])
        self.assertEqual(session.receipt()['refs'],[])
        self.assertEqual(session.work,work)
        pending={'evidence':[{'ref':ref}]}
        self.assertTrue(session.validate_pending([pending])['valid'])
        self.live['events'][0]['text']='Changed statement'
        check=session.validate_pending([pending])
        self.assertTrue(check['valid']);self.assertIn('observation_changed',check['warnings'])

    def test_unknown_unavailable_quantity_is_not_treated_as_supplied_source_content(self):
        session=EvidenceService(None,lambda:self.live).start()
        result=session.tool('evidence_query',dict(scope={'sources':['retained']},view='aggregates'))
        self.assertFalse(result['population']['complete'])
        self.assertTrue(session.validate_pending([result])['valid'])
        session.mark_supplied(result)
        self.assertTrue(session.validate()['valid'])

    def test_fresh_exact_lookup_distinguishes_removed_unknown_and_store_identity(self):
        keep=self.seed(chat='x:keep')[0];drop=self.seed(chat='x:drop')[0]
        session=self.service.start();store=session.generation()['store']
        before=session.resolve('deck:'+keep)
        self.archive.delete(chat_id='x:drop')
        exact=self.archive.evidence_current([keep,drop],store)
        self.assertTrue(exact['available']);self.assertEqual(exact['missing'],[drop])
        assessment=session.assess_refs(['deck:'+keep,'deck:'+drop])
        self.assertEqual(assessment['refs']['deck:'+keep]['status'],'supported')
        self.assertEqual(assessment['refs']['deck:'+drop]['status'],'removed')
        self.assertEqual(assessment['refs']['deck:'+keep]['record']['text'],before['text'])
        wrong=self.archive.evidence_current([keep],'wrong-store')
        self.assertFalse(wrong['available']);self.assertEqual(wrong['missing'],[])
        with patch.object(self.archive,'_verify',side_effect=OSError('Unavailable')):
            unknown=self.archive.evidence_current([drop],store)
        self.assertFalse(unknown['available']);self.assertEqual(unknown['missing'],[])

    def test_ingest_fence_survives_erasure_while_exact_assessment_is_current(self):
        first=self.seed(chat='x:keep')[0];self.seed(chat='x:drop')
        session=self.service.start();late=self.seed(chat='x:later',offset=-1)[0]
        self.archive.delete(chat_id='x:drop')
        result=session.tool('evidence_query',dict(scope={'sources':['retained']}))
        self.assertEqual({r['ref'] for r in result['evidence']},{'deck:'+first})
        fresh=self.archive.evidence_current([first,late],session.generation()['store'])
        self.assertEqual(set(fresh['records']),{first,late})

    def test_surviving_citation_does_not_hide_tainted_uncited_archive_input(self):
        keep=self.seed(chat='x:keep')[0];self.seed(chat='x:drop')
        session=self.service.start();result=self.query(session)
        session.mark_supplied(result)
        original_fp=session.original_fingerprints()['deck:'+keep]
        self.archive.delete(chat_id='x:drop')
        assessment=session.assess_refs(['deck:'+keep],{'deck:'+keep:original_fp})
        self.assertEqual(assessment['refs']['deck:'+keep]['status'],'supported')
        self.assertTrue(assessment['erasure_changed'])
        self.assertTrue(session.validate()['needs_fallback'])
        self.assertFalse(session.validate_pending([])['valid'])
        self.assertTrue(session.receipt()['archive_used'])
        self.assertEqual(session.archive_input_generation(),0)

    def test_expiry_is_confirmed_removed_and_requires_fresh_fallback(self):
        ref='deck:'+self.seed(offset=-29*86400)[0]
        session=self.service.start();result=self.query(session);session.mark_supplied(result)
        self.now+=2*86400
        assessment=session.assess_refs([ref])
        self.assertEqual(assessment['refs'][ref]['status'],'removed')
        self.assertTrue(session.validate()['needs_fallback'])

    def test_committed_erasure_hint_survives_storage_unavailability(self):
        self.seed();session=self.service.start();result=self.query(session);session.mark_supplied(result)
        self.archive.delete(all=True)
        with patch.object(self.archive,'_verify',side_effect=OSError('Unavailable')):
            identity=session.current_generation()
            self.assertEqual(identity['store'],session.generation()['store'])
            self.assertGreater(identity['erasure'],session.generation()['erasure'])
            self.assertTrue(session.validate()['needs_fallback'])
            fresh=self.service.start()
            self.assertEqual(fresh.generation()['erasure'],identity['erasure'])
            self.assertEqual(fresh.assess_refs(['deck:'+'d'*64])['refs']['deck:'+'d'*64]['status'],'unavailable')

    def test_read_after_erasure_has_its_own_input_epoch(self):
        self.seed(chat='x:keep');self.seed(chat='x:drop')
        session=self.service.start();self.archive.delete(chat_id='x:drop')
        result=self.query(session)
        pending=session.validate_pending([result])
        self.assertTrue(pending['valid'])
        self.assertEqual(pending['warnings'],[])
        session.mark_supplied(result)
        self.assertEqual(session.archive_input_generation(),1)
        check=session.validate()
        self.assertTrue(check['valid'])
        self.assertEqual(check['warnings'],[])
        self.assertEqual(check['assessment']['status'],'supported')
        self.assertEqual(session.receipt()['archive_input_generation'],1)

    def test_receipt_fingerprints_are_captured_before_values_can_change(self):
        self.live['events']=[dict(seq=1,chat_id='x:live',kind='message',role='assistant',text='Original source statement',certainty='recorded',source_ts=self.now,observed_ts=self.now)]
        session=self.service.start()
        hash_record=lambda row:__import__('hashlib').sha256(json.dumps(row,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        session.receipt_fingerprinter=hash_record
        result=session.tool('evidence_query',dict(scope={'sources':['live']}))
        ref=result['evidence'][0]['ref'];proof=session.pending[ref]['receipt_fingerprint']
        self.assertEqual(proof,hash_record(session.resolve(ref)))
        self.live['events'][0]['text']='Updated source statement'
        self.assertNotEqual(proof,hash_record(session.resolve(ref)))
        self.assertNotIn('text',session.pending[ref])
        session.mark_supplied(result)
        assessment=session.assess_refs([ref],{ref:proof},hash_record)
        self.assertEqual(assessment['refs'][ref]['status'],'stale')
        fact_result=session.tool('room_snapshot',dict(scope={'sources':['live']},fields=['model']))
        fact=fact_result['facts'][0]
        self.assertEqual(session.pending[fact['ref']]['receipt_fingerprint'],hash_record(session.original_observation(fact['ref'])))

    def test_unavailable_archive_qualifies_without_inventing_removal(self):
        ref='deck:'+self.seed()[0];session=self.service.start();result=self.query(session);session.mark_supplied(result)
        with patch.object(self.archive,'_verify',side_effect=OSError('Unavailable')):
            check=session.validate()
        self.assertTrue(check['valid']);self.assertFalse(check['needs_fallback'])
        self.assertEqual(check['assessment']['refs'][ref]['status'],'unavailable')
        self.assertFalse(check['assessment']['erasure_changed'])

    def test_invalid_inputs_never_become_sql_or_paths(self):
        session=self.service.start()
        for inp in [dict(sql='SELECT * FROM events'),dict(scope={'chat_ids':["' OR 1=1"]}),dict(filters={'file_ref':['../private']}),dict(interval={'since':float('nan')}),dict(match={'text':'x','mode':'regex'}),dict(limit=True)]:
            self.assertIn('error',session.tool('evidence_query',inp))


if __name__=='__main__':unittest.main()
