"""Clearly labeled synthetic data for the local, read-only demo."""
import random
import time

def seed(store):
    if store.rows("SELECT id FROM members WHERE guild='demo'"):
        return
    rng=random.Random(42)
    now=time.time()
    names=['Bobby','Luna','Nova','Kai','River','Atlas','Sunny','Sky','Alex','Sage','Ash','Morgan']
    for i,name in enumerate(names):
        store.member('demo',str(i+1),name,[],now-90*86400,now-800*86400)
    channels=['general','introductions','pineapple-chat','gaming','voice-lounge']
    for i,name in enumerate(channels):
        store.execute('INSERT INTO channels VALUES(?,?,?)',('demo',str(100+i),name))
    for day in range(30):
        ts=now-day*86400
        for j in range(rng.randrange(90,350)):
            store.message(f'demo-{day}-{j}','demo',rng.randrange(1,13),rng.randrange(100,104),ts-rng.randrange(86400))
        for m in range(1,13):
            begin=ts-rng.randrange(86400)
            store.span('demo',m,104,'voice','voice',begin,begin+rng.randrange(300,7200))
            store.span('demo',m,'','activity',rng.choice(['Minecraft','Spotify','Overwatch 2']),begin,begin+1800)
            store.span('demo',m,'','status',rng.choice(['online','idle','dnd']),begin,begin+3600)
        store.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?)',('demo',ts,180+30-day,40,15,8,117))
    store.object('demo','rule',{'name':'Active Pineapple','role_id':'200','enabled':True,'condition':{'metric':'messages','operator':'>=','value':100,'days':14},'interval_seconds':300})
    store.object('demo','counter',{'name':'Member counter','channel_id':'104','metric':'members','template':'🍍 Members: {value}','enabled':True,'interval_seconds':600})
