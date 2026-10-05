import logging
import csv
import hashlib
import io
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo
import discord
import httpx
from croniter import croniter
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from bot import validate_condition
from store import DEFAULTS

BASE=Path(__file__).parent
API='https://discord.com/api/v10'

def create_app(store, bot=None, demo=False):
    app=FastAPI(title='Pineapple Stats',docs_url=None,redoc_url=None)
    sessions={}
    states={}
    public=os.getenv('PUBLIC_URL','http://localhost:8000').rstrip('/')
    secure=public.startswith('https://')
    callback=public+'/auth/callback'
    client_id=os.getenv('DISCORD_CLIENT_ID','')
    client_secret=os.getenv('DISCORD_CLIENT_SECRET','')
    app.state.sessions=sessions
    app.state.store=store

    @app.middleware('http')
    async def headers(request,call_next):
        if request.method in ('POST','PUT','PATCH','DELETE'):
            if request.headers.get('origin') != public:
                return JSONResponse({'detail':'Request origin is not allowed'},status_code=403)
            if int(request.headers.get('content-length','0'))>262144:
                return JSONResponse({'detail':'Request is too large'},status_code=413)
        result=await call_next(request)
        result.headers['X-Content-Type-Options']='nosniff'
        result.headers['Referrer-Policy']='same-origin'
        result.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith(('/api','/auth')):
            result.headers['Cache-Control']='no-store'
        return result

    def session(request):
        sid=request.cookies.get('ps_session','')
        data=sessions.get(sid)
        if not data or data['expires']<time.time():
            sessions.pop(sid,None)
            raise HTTPException(401,'Log in with Discord')
        return data

    async def discord_get(path,token):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response=await client.get(API+path,headers={'Authorization':'Bearer '+token})
            if response.status_code in (401,403):
                raise HTTPException(401,'Discord authorization expired. Log in again.')
            if response.status_code == 429:
                logging.getLogger('pineapple.web').warning('Discord rate limited dashboard lookup: %s',path)
                raise HTTPException(503,'Discord rate limited the dashboard. Wait a moment, then refresh.')
            if response.status_code == 404:
                raise HTTPException(403,'You are no longer a member of this server.')
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as error:
            status=getattr(getattr(error,'response',None),'status_code',None)
            logging.getLogger('pineapple.web').warning('Discord dashboard lookup failed: %s status=%s error=%s',path,status,type(error).__name__)
            raise HTTPException(503,'Unable to verify Discord access right now. Please try again shortly.')

    async def access(request,guild_id,edit=False,owner=False):
        # API keys are read-only and restricted to one guild.
        authorization=request.headers.get('authorization','')
        if authorization.startswith('Bearer ps_') and not edit and not owner:
            hashed=hashlib.sha256(authorization[7:].encode()).hexdigest()
            row=store.db.execute('SELECT guild FROM keys WHERE hash=?',(hashed,)).fetchone()
            if row and row[0]==guild_id:
                return {'user':{'id':'api'},'level':'view'}
            raise HTTPException(403,'Invalid API key for this server')
        data=session(request)
        if demo:
            if guild_id!='demo':
                raise HTTPException(404,'Server not found')
            if edit or owner:
                raise HTTPException(403,'Demo is read-only')
            return {**data,'level':'admin'}
        guild=bot.get_guild(int(guild_id)) if bot and guild_id.isdigit() else None
        if not guild:
            raise HTTPException(404,'The bot is not connected to this server')
        # The connected Gateway keeps member roles and departures current.
        # Reuse that live state instead of calling the limited OAuth endpoint
        # for every graph and two-second dashboard refresh.
        get_member=getattr(guild,'get_member',None)
        ready=getattr(bot,'is_ready',lambda:False)()
        member=get_member(int(data['user']['id'])) if ready and get_member else None
        if member is not None:
            me={'roles':[str(role.id) for role in member.roles]}
        else:
            me=await discord_get('/users/@me/guilds/'+guild_id+'/member',data['token'])
        s=store.settings(guild_id)
        roles=set(me.get('roles',[]))
        permissions=0
        everyone=guild.default_role
        permissions|=everyone.permissions.value
        for role_id in roles:
            role=guild.get_role(int(role_id))
            if role:
                permissions|=role.permissions.value
        admin=str(guild.owner_id)==data['user']['id'] or bool(permissions & 8)
        manager=admin or (s['elevate_manage_server'] and bool(permissions & 32))
        moderation=discord.Permissions(moderate_members=True,kick_members=True,ban_members=True,manage_messages=True)
        moderator=bool(permissions & moderation.value)
        editor=manager or moderator or bool(roles & set(s['editor_roles']))
        viewer=editor or s['dashboard_access']=='members' or bool(roles & set(s['viewer_roles']))
        if not viewer or edit and not editor or owner and not admin:
            raise HTTPException(403,'You do not have permission for this action')
        return {**data,'level':'admin' if admin else 'edit' if editor else 'view'}

    @app.get('/health')
    async def health():
        return {'web':'ok','bot_connected':bool(bot and bot.is_ready()),'demo':demo}

    @app.get('/auth/login')
    async def login():
        now=time.time()
        for sid in list(sessions):
            if sessions[sid]['expires']<now:
                del sessions[sid]
        for k in list(states):
            if states[k]<now:
                del states[k]
        if demo:
            sid=secrets.token_urlsafe(32)
            sessions[sid]={'user':{'id':'demo','username':'Demo viewer'},'expires':now+3600}
            response=RedirectResponse('/')
            response.set_cookie('ps_session',sid,httponly=True,secure=secure,samesite='lax',max_age=3600)
            return response
        if not client_id or not client_secret:
            raise HTTPException(503,'Set the Discord application ID and OAuth2 secret first')
        state=secrets.token_urlsafe(32)
        states[state]=now+600
        response=RedirectResponse('https://discord.com/oauth2/authorize?'+urlencode({
            'client_id':client_id,'redirect_uri':callback,'response_type':'code',
            'scope':'identify guilds guilds.members.read','state':state}))
        response.set_cookie('ps_oauth_state',state,httponly=True,secure=secure,samesite='lax',max_age=600)
        return response

    @app.get('/auth/callback')
    async def oauth_callback(request:Request,code:str='',state:str=''):
        cookie=request.cookies.get('ps_oauth_state','')
        if not state or not secrets.compare_digest(state,cookie) or states.pop(state,0)<time.time():
            raise HTTPException(400,'Login expired or invalid. Please log in again.')
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response=await client.post(API+'/oauth2/token',data={
                    'client_id':client_id,'client_secret':client_secret,'grant_type':'authorization_code',
                    'code':code,'redirect_uri':callback})
                response.raise_for_status()
                token=response.json()
            user=await discord_get('/users/@me',token['access_token'])
        except httpx.HTTPError:
            raise HTTPException(400,'Discord login failed. Check the redirect URL and application settings.')
        sid=secrets.token_urlsafe(32)
        lifetime=min(86400,int(token.get('expires_in',3600)))
        sessions[sid]={'user':{'id':user['id'],'username':user['username']},
                       'token':token['access_token'],'expires':time.time()+lifetime}
        result=RedirectResponse('/')
        result.set_cookie('ps_session',sid,httponly=True,secure=secure,samesite='lax',max_age=lifetime)
        result.delete_cookie('ps_oauth_state')
        return result

    @app.post('/auth/logout')
    async def logout(request:Request):
        sessions.pop(request.cookies.get('ps_session',''),None)
        result=JSONResponse({'ok':True})
        result.delete_cookie('ps_session')
        return result

    @app.get('/api/me')
    async def me(request:Request):
        data=session(request)
        if demo:
            return {'user':data['user'],'guilds':[{'id':'demo','name':'Pineapple Planet · Demo'}],'demo':True,'invite_url':None}
        guilds=await discord_get('/users/@me/guilds',data['token'])
        available=[]
        for guild in guilds:
            if bot and bot.get_guild(int(guild['id'])):
                try:
                    auth=await access(request,guild['id'])
                    available.append({'id':guild['id'],'name':guild['name'],'level':auth['level']})
                except HTTPException as error:
                    if error.status_code not in (403,404):
                        raise
        # View/history, Manage Roles, Manage Channels, Manage Guild (invite reads), attachments.
        permissions=discord.Permissions(view_channel=True,read_message_history=True,send_messages=True,
            embed_links=True,attach_files=True,manage_roles=True,manage_channels=True,manage_guild=True)
        invite='https://discord.com/oauth2/authorize?'+urlencode({'client_id':client_id,
            'scope':'bot applications.commands','permissions':permissions.value})
        return {'user':data['user'],'guilds':available,'demo':False,'invite_url':invite}

    @app.get('/api/guilds/{guild_id}/directory')
    async def directory(request:Request,guild_id:str):
        auth=await access(request,guild_id)
        guild=bot.get_guild(int(guild_id)) if not demo else None
        return {'level':auth['level'],'members':store.rows('SELECT id,name,roles,present FROM members WHERE guild=?',(guild_id,)),
            'channels':store.rows('SELECT id,name FROM channels WHERE guild=?',(guild_id,)),
            'roles':[{'id':str(r.id),'name':r.name} for r in guild.roles] if guild else []}

    def query(request,guild_id):
        q=request.query_params
        now=time.time()
        try:
            start=float(q.get('start',now-store.settings(guild_id)['lookback_days']*86400))
            end=float(q.get('end',now))
            if not 0<=start<end<=now+300:
                raise ValueError()
            return dict(start=start,end=end,metric=q.get('metric','messages'),interval=q.get('interval','day'),
                members=[x for x in q.get('members','').split(',') if x],
                channels=[x for x in q.get('channels','').split(',') if x],
                roles=[x for x in q.get('roles','').split(',') if x],
                activities=[x.strip() for x in q.get('activities','').split(',') if x.strip()],
                selection_mode=q.get('selection_mode','include'), time_unit=q.get('time_unit','minutes'))
        except ValueError:
            raise HTTPException(422,'Invalid date range')

    @app.get('/api/guilds/{guild_id}/live')
    async def live(request:Request, guild_id:str):
        await access(request, guild_id)
        now = time.time()
        guild = bot.get_guild(int(guild_id)) if bot and guild_id.isdigit() else None
        if guild:
            members = guild.member_count or len(guild.members)
            online = sum(m.status != discord.Status.offline for m in guild.members)
            voice = sum(len(c.members) for c in guild.voice_channels)
        else:
            row = store.rows('SELECT * FROM snapshots WHERE guild=? ORDER BY ts DESC LIMIT 1', (guild_id,))
            members = row[0]['members'] if row else 0
            online = row[0]['online'] + row[0]['idle'] + row[0]['dnd'] if row else 0
            voice = 0
        messages = store.db.execute('SELECT count(*) FROM messages WHERE guild=? AND ts>=?', (guild_id,now-86400)).fetchone()[0]
        active_ids={c['id'] for c in store.objects(guild_id,'counter') if c.get('enabled',True)}
        counters = [dict(v) for k,v in getattr(bot,'counter_status',{}).items() if str(k[0]) == guild_id and v.get('id') in active_ids]
        return {'ts':now, 'connected':bool(guild and bot.is_ready()), 'demo':demo,
            'members':members, 'online':online, 'in_voice':voice, 'messages_24h':messages, 'counters':counters}

    @app.get('/api/guilds/{guild_id}/stats')
    async def stats(request:Request,guild_id:str):
        await access(request,guild_id)
        try:
            params=query(request,guild_id)
            result=store.summary(guild_id,**params)
            if request.query_params.get('compare') == 'true':
                duration=params['end']-params['start']
                previous=store.summary(guild_id,**{**params,'start':max(0,params['start']-duration),'end':params['start']}) if params['start'] else {'total':0,'series':[]}
                result['previous']=previous
                result['change']=round(result['total']-previous['total'],3)
                result['change_percent']=round(result['change']/previous['total']*100,2) if previous['total'] else None
            return result
        except ValueError as error:
            raise HTTPException(422,str(error))

    @app.get('/api/guilds/{guild_id}/export')
    async def export(request:Request,guild_id:str):
        await access(request,guild_id)
        try:
            data=store.summary(guild_id,**query(request,guild_id))
        except ValueError as error:
            raise HTTPException(422,str(error))
        group=request.query_params.get('group','members')
        if group not in ('members','channels','labels','series'):
            raise HTTPException(422,'Unknown export group')
        rows=data[group]
        output=io.StringIO()
        fields=['bucket','value'] if group=='series' else ['id','name','value']
        writer=csv.DictWriter(output,fieldnames=fields)
        writer.writeheader()
        for row in rows:
            # Protect spreadsheet users against formula injection in Discord names.
            writer.writerow({k:"'"+v if isinstance(v,str) and v.startswith(('=','+','-','@','\t','\r','\n')) else v for k,v in row.items()})
        return Response(output.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="pineapple-stats.csv"'})

    @app.get('/api/guilds/{guild_id}/snapshots')
    async def snapshots(request:Request,guild_id:str):
        await access(request,guild_id)
        q=query(request,guild_id)
        return store.rows('SELECT * FROM snapshots WHERE guild=? AND ts>=? AND ts<? ORDER BY ts',(guild_id,q['start'],q['end']))

    @app.get('/api/guilds/{guild_id}/invites')
    async def invites(request:Request,guild_id:str):
        await access(request,guild_id)
        q=query(request,guild_id)
        rows=store.rows('SELECT * FROM invites WHERE guild=? AND ts>=? AND ts<? ORDER BY ts DESC',(guild_id,q['start'],q['end']))
        s=store.settings(guild_id)
        for row in rows:
            row['qualification']='invalid' if row['reason'] else 'pending' if time.time()-row['ts']<s['invite_min_join_days']*86400 else 'valid'
        return rows

    @app.get('/api/guilds/{guild_id}/settings')
    async def settings(request:Request,guild_id:str):
        await access(request,guild_id)
        return store.settings(guild_id)

    @app.put('/api/guilds/{guild_id}/settings')
    async def save_settings(request:Request,guild_id:str):
        auth=await access(request,guild_id,edit=True)
        data=await request.json()
        permissions={'dashboard_access','viewer_roles','editor_roles','elevate_manage_server','command_access','command_channels'}
        if permissions & set(data) and auth['level']!='admin':
            raise HTTPException(403,'Only administrators can change access permissions')
        if set(data)-set(DEFAULTS):
            raise HTTPException(422,'Unknown setting')
        try:
            merged={**store.settings(guild_id),**data}
            ZoneInfo(merged['timezone'])
            if merged['dashboard_access'] not in ('admins','members') or merged['command_access'] not in ('admins','members'):
                raise ValueError('Access must be admins or members')
            if merged['filter_mode'] not in ('include','exclude') or merged['interval'] not in ('half_hour','hour','day','week','month'):
                raise ValueError('Invalid filter mode or interval')
            if not 1<=int(merged['lookback_days'])<=36500:
                raise ValueError('Lookback must be between 1 and 36500 days')
            for key in ('invite_min_account_days','invite_min_join_days'):
                if not 0<=float(merged[key])<=36500:
                    raise ValueError('Invalid invite age threshold')
            for key,value in merged.items():
                if isinstance(DEFAULTS[key],bool) and not isinstance(value,bool):
                    raise ValueError(f'{key} must be true or false')
                if isinstance(DEFAULTS[key],list) and (not isinstance(value,list) or any(not isinstance(x,str) for x in value)):
                    raise ValueError(f'{key} must be a list of IDs as strings')
            aggregate=merged['aggregate_channels']
            if not isinstance(aggregate,dict) or any(not isinstance(v,list) or any(not isinstance(x,str) for x in v) for v in aggregate.values()):
                raise ValueError('Aggregate channels must map target IDs to lists of source IDs')
            sources=[x for v in aggregate.values() for x in v]
            if len(sources)!=len(set(sources)) or set(sources)&set(aggregate):
                raise ValueError('Aggregate sources must be unique and cannot also be targets')
            store.save_settings(guild_id,data)
        except (ValueError,TypeError,KeyError) as error:
            raise HTTPException(422,str(error))
        store.log(guild_id,auth['user']['id'],'settings_updated',data)
        return {'ok':True}

    def validate_object(kind,data,guild):
        if not isinstance(data,dict):
            raise ValueError('Expected an object')
        data.pop('id',None)
        if kind=='rule':
            validate_condition(data['condition'])
            if data.get('cron') and not croniter.is_valid(data['cron']):
                raise ValueError('Invalid five-field cron expression')
            if data.get('cron') and len(data['cron'].split())!=5:
                raise ValueError('Use a five-field cron expression')
            role=guild.get_role(int(data['role_id']))
            if not role or role.is_default() or role.managed or role >= guild.me.top_role:
                raise ValueError('Select a regular role below the bot role')
            sensitive=discord.Permissions(administrator=True,manage_guild=True,manage_roles=True,manage_channels=True,
                ban_members=True,kick_members=True,manage_webhooks=True,moderate_members=True)
            if role.permissions.value & sensitive.value:
                raise ValueError('Automatic rules cannot grant administrative or moderation roles')
            if data.get('top_n') is not None and not 1<=int(data['top_n'])<=100000:
                raise ValueError('Top limit must be at least 1')
            if data.get('top_metric','messages') not in ('messages','voice','activity','invites'):
                raise ValueError('Invalid top metric')
            if not 1<=float(data.get('days',14))<=36500:
                raise ValueError('Invalid rule lookback')
            if not 60<=int(data.get('interval_seconds',300))<=31536000:
                raise ValueError('Role interval must be 60 seconds to one year')
        if kind=='counter':
            if not guild.get_channel(int(data['channel_id'])):
                raise ValueError('Select an existing channel in this server')
            if data.get('metric') not in ('members','humans','bots','online','roles','channels','boosts','clock','youtube','tiktok','role_members','messages','voice','activity','status','invites'):
                raise ValueError('Unknown counter type')
            if '{value}' not in data.get('template',''):
                raise ValueError('Counter template must contain {value}')
            if not 1<=float(data.get('days',14))<=36500:
                raise ValueError('Counter days must be 1–36500')
            if data.get('operation','total') not in ('total','average','change','top','unique'):
                raise ValueError('Unknown counter operation')
            if data.get('timezone'):
                ZoneInfo(data['timezone'])
            if data.get('update_mode','live') not in ('live','scheduled'):
                raise ValueError('Counter mode must be live or scheduled')
            if not 5<=int(data.get('interval_seconds') or 5)<=31536000:
                raise ValueError('Scheduled counter interval must be at least 5 seconds')
            if data.get('group','members') not in ('members','channels','labels'):
                raise ValueError('Invalid counter ranking group')
            if data.get('metric')=='role_members' and not guild.get_role(int(data.get('role_id',0))):
                raise ValueError('Select a valid role ID for this counter')
            if data.get('youtube_stat','subscriberCount') not in ('subscriberCount','viewCount','videoCount'):
                raise ValueError('Invalid YouTube statistic')
            for key in ('members','channels','roles','activities'):
                value=data.get(key,[])
                if not isinstance(value,list) or any(not isinstance(x,str) for x in value):
                    raise ValueError('Counter filters must be arrays of strings')
        if kind=='panel':
            if not data.get('name') or data.get('metric') not in ('messages','voice','activity','status','invites'):
                raise ValueError('Panel requires a name and supported metric')
            if data.get('view','metric') not in ('metric','line','bar','area','heatmap','pie'):
                raise ValueError('Invalid panel view')
            if not isinstance(data.get('query',{}),dict):
                raise ValueError('Panel query must be an object')
            if not 1 <= float(data.get('days',14)) <= 3650:
                raise ValueError('Panel lookback must be 1–3650 days')
        if kind=='preset':
            if not isinstance(data.get('query'),dict) or not data.get('name'):
                raise ValueError('Preset requires name and query')

    @app.get('/api/guilds/{guild_id}/objects/{kind}')
    async def objects(request:Request,guild_id:str,kind:str):
        await access(request,guild_id)
        if kind not in ('rule','counter','preset','panel'):
            raise HTTPException(404)
        return store.objects(guild_id,kind)

    @app.post('/api/guilds/{guild_id}/objects/{kind}')
    @app.put('/api/guilds/{guild_id}/objects/{kind}/{object_id}')
    async def save_object(request:Request,guild_id:str,kind:str,object_id:int=None):
        auth=await access(request,guild_id,edit=True)
        if kind not in ('rule','counter','preset','panel'):
            raise HTTPException(404)
        if object_id and not store.rows('SELECT id FROM objects WHERE id=? AND guild=? AND kind=?',(object_id,guild_id,kind)):
            raise HTTPException(404)
        data=await request.json()
        try:
            guild=bot.get_guild(int(guild_id))
            validate_object(kind,data,guild)
            if kind=='rule' and auth['level']!='admin':
                member=guild.get_member(int(auth['user']['id']))
                if not member or guild.get_role(int(data['role_id']))>=member.top_role:
                    raise ValueError('You can only automate roles below your highest role')
        except (ValueError,KeyError,TypeError,AttributeError) as error:
            raise HTTPException(422,str(error))
        oid=store.object(guild_id,kind,data,object_id)
        store.log(guild_id,auth['user']['id'],kind+'_saved',{'id':oid,'data':data})
        return {'id':oid}

    @app.delete('/api/guilds/{guild_id}/objects/{kind}/{object_id}')
    async def delete_object(request:Request,guild_id:str,kind:str,object_id:int):
        auth=await access(request,guild_id,edit=True)
        store.execute('DELETE FROM objects WHERE id=? AND guild=? AND kind=?',(object_id,guild_id,kind))
        store.log(guild_id,auth['user']['id'],kind+'_deleted',{'id':object_id})
        return {'ok':True}

    @app.post('/api/guilds/{guild_id}/history')
    async def history(request:Request,guild_id:str):
        auth=await access(request,guild_id,edit=True)
        data=await request.json()
        try:
            start,end=float(data['start']),float(data['end'])
            channels=data['channels']
            if not isinstance(channels,list) or not 0<=start<end<=time.time()+300:
                raise ValueError()
        except (KeyError,TypeError,ValueError):
            raise HTTPException(422,'Choose channels and a valid date range')
        if any(j['guild']==guild_id and j['status']=='running' for j in bot.jobs.values()):
            raise HTTPException(409,'A history import is already running in this server')
        job=await bot.sync_history(bot.get_guild(int(guild_id)),channels,start,end)
        store.log(guild_id,auth['user']['id'],'history_started',{'job':job['id']})
        return job

    @app.get('/api/guilds/{guild_id}/jobs')
    async def jobs(request:Request,guild_id:str):
        await access(request,guild_id,edit=True)
        return [j for j in bot.jobs.values() if j['guild']==guild_id]

    @app.get('/api/guilds/{guild_id}/audit')
    async def audit(request:Request,guild_id:str):
        await access(request,guild_id,edit=True)
        return store.rows('SELECT * FROM audit WHERE guild=? ORDER BY id DESC LIMIT 500',(guild_id,))

    @app.get('/api/guilds/{guild_id}/keys')
    async def keys(request:Request,guild_id:str):
        await access(request,guild_id,owner=True)
        return store.rows('SELECT substr(hash,1,12) AS fingerprint,created FROM keys WHERE guild=?',(guild_id,))

    @app.post('/api/guilds/{guild_id}/keys')
    async def create_key(request:Request,guild_id:str):
        auth=await access(request,guild_id,owner=True)
        key='ps_'+secrets.token_urlsafe(32)
        store.execute('INSERT INTO keys VALUES(?,?,?)',(hashlib.sha256(key.encode()).hexdigest(),guild_id,time.time()))
        store.log(guild_id,auth['user']['id'],'api_key_created',{})
        return {'key':key,'note':'Copy now. This key will not be shown again.'}

    @app.delete('/api/guilds/{guild_id}/keys')
    async def revoke_keys(request:Request,guild_id:str):
        auth=await access(request,guild_id,owner=True)
        store.execute('DELETE FROM keys WHERE guild=?',(guild_id,))
        store.log(guild_id,auth['user']['id'],'api_keys_revoked',{})
        return {'ok':True}

    app.mount('/assets',StaticFiles(directory=BASE/'web'),name='assets')
    @app.get('/')
    async def home():
        return FileResponse(BASE/'web'/'index.html')
    return app
