"""Seed a pre-gate durable record in disposable test storage, never a live store."""
import time
import claude_channel


def seed_legacy_claude(store, agent, question, request_id, agents):
    descriptor = claude_channel.discover(store.channel_root, agent[2:])
    row = dict(request_id=request_id, agent=agent, name=agents[agent]['name'],
               question=question, state='sending', reply='', error='', created=time.time(),
               launched=False, runtime='claude', channel=claude_channel.identity(descriptor),
               envelope=claude_channel.envelope(request_id, question))
    store._save(row)
    return store._public_request(row)
