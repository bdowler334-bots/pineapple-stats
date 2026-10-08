import asyncio
import io
import logging
import math
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import discord
import httpx
from discord import app_commands
from discord.ext import commands, tasks
from croniter import croniter

log = logging.getLogger('pineapple.bot')

def conditions_match(node, resolve):
    """Nested AND/OR groups, numeric comparisons, negation, and rule references."""
    if 'conditions' in node:
        values = [conditions_match(n, resolve) for n in node['conditions']]
        result = all(values) if node.get('match','all') == 'all' else any(values)
    else:
        value = resolve(node)
        target = float(node.get('value', 0))
        operation = node.get('operator', '>=')
        result = {'>=': lambda:value>=target, '>': lambda:value>target,
                  '<=': lambda:value<=target, '<': lambda:value<target,
                  '==': lambda:value==target, '!=': lambda:value!=target}[operation]()
    return not result if node.get('invert',False) else result

def validate_condition(node, depth=0):
    if depth > 8:
        raise ValueError('Conditions may be nested at most eight levels')
    if 'conditions' in node:
        if node.get('match','all') not in ('all','any') or not 1 <= len(node['conditions']) <= 30:
            raise ValueError('Use all/any and one to thirty conditions per group')
        for child in node['conditions']:
            validate_condition(child,depth+1)
        return
    if node.get('metric') not in ('messages','voice','activity','invites','account_age','joined_age','role','statrole','compare'):
        raise ValueError('Unknown condition metric')
    if node.get('operator','>=') not in ('>=','>','<=','<','==','!='):
        raise ValueError('Unknown comparison')
    float(node.get('value',0))
    if not 0 <= float(node.get('days',14)) <= 36500:
        raise ValueError('Condition days must be 0–36500')
    if node.get('metric') == 'compare':
        if node.get('left','messages') not in ('messages','voice','activity','invites') or node.get('right','voice') not in ('messages','voice','activity','invites'):
            raise ValueError('Compare supports messages, voice, activity and invites')

class StatsBot(commands.Bot):
    def __init__(self, store):
        intents = discord.Intents.default()
        intents.members = True
        intents.presences = True
        # Counting messages does not require their text or Message Content intent.
        super().__init__(command_prefix=commands.when_mentioned, intents=intents,
                         allowed_mentions=discord.AllowedMentions.none())
        self.store = store
        self.active = {}
        self.invite_cache = {}
        self.jobs = {}
        self.last_rules = {}
        self.last_counters = {}
        self.counter_tasks = {}
        self.counter_status = {}
        self.social_cache = {}
        self.join_locks = {}
        self.synced = False
        self.register_commands()

    async def setup_hook(self):
        await self.tree.sync()
        self.synced = True

    def remember_member(self, member):
        self.store.member(member.guild.id, member.id, member.display_name,
                          [r.id for r in member.roles],
                          member.joined_at.timestamp() if member.joined_at else time.time(),
                          member.created_at.timestamp())

    def included(self, member):
        return not member.bot or self.store.settings(member.guild.id)['include_bots']

    def voice_eligible(self, member):
        state = member.voice
        if not state or not state.channel or not self.included(member):
            return False
        s = self.store.settings(member.guild.id)
        return ((s['track_voice_alone'] or len([m for m in state.channel.members if self.included(m)]) > 1)
                and (s['track_muted'] or not (state.mute or state.self_mute))
                and (s['track_deafened'] or not (state.deaf or state.self_deaf))
                and state.channel != member.guild.afk_channel)

    def reconcile(self, member):
        now = time.time()
        wanted = {}
        if self.included(member):
            if self.voice_eligible(member):
                wanted['voice'] = (str(member.voice.channel.id),'voice')
            if member.status != discord.Status.offline:
                wanted['status'] = ('',str(member.status))
            if self.store.settings(member.guild.id)['activity_tracking']:
                for a in member.activities:
                    if a.type in (discord.ActivityType.playing,discord.ActivityType.streaming,
                                  discord.ActivityType.listening,discord.ActivityType.watching,discord.ActivityType.competing):
                        name = a.name or 'Unknown'
                        wanted['activity:'+name] = ('',name)
        keys = [k for k in self.active if k[0]==member.guild.id and k[1]==member.id]
        for key in keys:
            channel,label,start = self.active[key]
            if wanted.get(key[2]) != (channel,label):
                self.store.span(key[0],key[1],channel,key[2].split(':')[0],label,start,now)
                del self.active[key]
        for kind, (channel,label) in wanted.items():
            self.active.setdefault((member.guild.id,member.id,kind),(channel,label,now))

    def flush(self):
        now = time.time()
        for key,(channel,label,start) in list(self.active.items()):
            self.store.span(key[0],key[1],channel,key[2].split(':')[0],label,start,now)
            self.active[key]=(channel,label,now)

    async def on_ready(self):
        self.active.clear()  # Reconnect gaps are not invented as activity.
        for guild in self.guilds:
            for member in guild.members:
                self.remember_member(member)
                self.reconcile(member)
            for channel in guild.channels:
                self.store.execute('INSERT OR REPLACE INTO channels VALUES(?,?,?)',
                                   (str(guild.id),str(channel.id),channel.name))
            await self.refresh_invites(guild)
        if not self.tick.is_running():
            self.tick.start()
        if not self.live_tick.is_running():
            self.live_tick.start()
        log.info('Connected as %s in %s servers', self.user, len(self.guilds))

    async def on_disconnect(self):
        self.flush()
        self.active.clear()

    async def on_guild_join(self, guild):
        for member in guild.members:
            self.remember_member(member)
            self.reconcile(member)
        for channel in guild.channels:
            await self.on_guild_channel_create(channel)
        await self.refresh_invites(guild)

    async def on_guild_remove(self, guild):
        for key in [k for k in self.active if k[0]==guild.id]:
            del self.active[key]

    async def on_message(self, message):
        if message.guild and isinstance(message.author,discord.Member) and self.included(message.author):
            parent = getattr(message.channel,'parent_id',None)
            self.remember_member(message.author)
            self.store.message(message.id,message.guild.id,message.author.id,parent or message.channel.id,
                               message.created_at.timestamp())

    async def on_presence_update(self, before, after):
        self.reconcile(after)

    async def on_voice_state_update(self, member, before, after):
        self.reconcile(member)
        for channel in {before.channel, after.channel} - {None}:
            for other in channel.members:
                self.reconcile(other)

    async def on_member_update(self, before, after):
        self.remember_member(after)

    async def on_guild_channel_create(self, channel):
        self.store.execute('INSERT OR REPLACE INTO channels VALUES(?,?,?)',
                           (str(channel.guild.id),str(channel.id),channel.name))

    async def on_guild_channel_update(self, before, after):
        await self.on_guild_channel_create(after)

    async def refresh_invites(self, guild):
        try:
            result = {i.code:(i.uses or 0,str(i.inviter.id) if i.inviter else None) for i in await guild.invites()}
            self.invite_cache[guild.id] = result
            return result
        except discord.HTTPException:
            return None

    async def on_invite_create(self, invite):
        await self.refresh_invites(invite.guild)

    async def on_invite_delete(self, invite):
        # Keep deleted invites until the next join comparison; one-use attribution remains ambiguous.
        pass

    async def on_member_join(self, member):
        self.remember_member(member)
        self.reconcile(member)
        lock = self.join_locks.setdefault(member.guild.id,asyncio.Lock())
        async with lock:
            previous = self.invite_cache.get(member.guild.id,{})
            current = await self.refresh_invites(member.guild)
            candidates = [] if current is None else [(code,v) for code,v in current.items()
                if code in previous and v[0] > previous[code][0]]
            # Multiple increments, batch joins, vanity and deleted invites must not be guessed.
            certain = len(candidates)==1 and candidates[0][1][0]-previous[candidates[0][0]][0]==1
            code,inviter = (candidates[0][0],candidates[0][1][1]) if certain else (None,None)
            reason = 'young_account' if (time.time()-member.created_at.timestamp())/86400 < self.store.settings(member.guild.id)['invite_min_account_days'] else None
            if code is None:
                reason = 'unknown_invite'
            self.store.execute('INSERT OR IGNORE INTO invites VALUES(?,?,?,?,?,NULL,?)',
                               (str(member.guild.id),str(member.id),code,inviter,time.time(),reason))

    async def on_member_remove(self, member):
        now = time.time()
        for key in [k for k in self.active if k[:2] == (member.guild.id,member.id)]:
            channel,label,start=self.active.pop(key)
            self.store.span(key[0],key[1],channel,key[2].split(':')[0],label,start,now)
        self.store.execute('UPDATE members SET present=0 WHERE guild=? AND id=?',(str(member.guild.id),str(member.id)))
        age = self.store.settings(member.guild.id)['invite_min_join_days'] * 86400
        self.store.execute("UPDATE invites SET left_at=?,reason=CASE WHEN ?-ts<? THEN 'left_early' ELSE reason END WHERE guild=? AND member=? AND left_at IS NULL",
                           (now,now,age,str(member.guild.id),str(member.id)))

    def invite_total(self, guild, inviter, start, end):
        s = self.store.settings(guild.id)
        total=0
        for r in self.store.rows('SELECT * FROM invites WHERE guild=? AND inviter=? AND ts>=? AND ts<?',
                                 (str(guild.id),str(inviter),start,end)):
            member = guild.get_member(int(r['member']))
            required = set(s['invite_required_roles'])
            if (r['reason'] is None and end-r['ts'] >= s['invite_min_join_days']*86400
                and (not required or member and required.issubset({str(x.id) for x in member.roles}))):
                total+=1
        return total

    def resolve(self, guild, member, condition, now):
        metric = condition['metric']
        if metric == 'account_age':
            return (now-member.created_at.timestamp())/86400
        if metric == 'joined_age':
            return (now-(member.joined_at.timestamp() if member.joined_at else now))/86400
        if metric in ('role','statrole'):
            role_id = str(condition.get('role_id',''))
            if metric == 'statrole':
                rules = self.store.objects(guild.id,'rule')
                rule = next((r for r in rules if str(r['id'])==str(condition.get('rule_id'))),None)
                role_id = str(rule['role_id']) if rule else ''
            return int(role_id in {str(r.id) for r in member.roles})
        start = now-float(condition.get('days',14))*86400
        if condition.get('start') is not None:
            start = float(condition['start'])
        stop = float(condition.get('end',now))
        if metric == 'invites':
            return self.invite_total(guild,member.id,start,stop)
        def value(kind):
            data = self.store.summary(guild.id,start,stop,kind,
                members=[str(member.id)],channels=condition.get('channels'),
                activities=condition.get('activities'),apply_defaults=False)
            return data['total']
        if metric == 'compare':
            def comparison_value(kind):
                return self.invite_total(guild,member.id,start,stop) if kind=='invites' else value(kind)
            left,right=comparison_value(condition.get('left','messages')),comparison_value(condition.get('right','voice'))
            return left-right if condition.get('mode','difference')=='difference' else left/right if right else 0
        return value(metric)

    async def apply_rules(self, guild):
        now=time.time()
        for rule in self.store.objects(guild.id,'rule'):
            if not rule.get('enabled',True):
                continue
            interval=max(60,int(rule.get('interval_seconds',300)))
            key=(guild.id,rule['id'])
            last=self.last_rules.get(key,0)
            cron=rule.get('cron','').strip()
            if cron:
                tz=ZoneInfo(self.store.settings(guild.id)['timezone'])
                if last and croniter(cron,datetime.fromtimestamp(last,tz)).get_next(float)>now:
                    continue
            elif now-last < interval:
                continue
            self.last_rules[key]=now
            role=guild.get_role(int(rule['role_id']))
            if not role or role.managed or role.is_default() or role >= guild.me.top_role or role.permissions.administrator:
                continue
            top_ids=None
            if rule.get('top_n'):
                metric=rule.get('top_metric','messages')
                data=self.store.summary(guild.id,now-float(rule.get('days',14))*86400,now,metric,apply_defaults=False)
                top_ids={r['id'] for r in data['members'][:int(rule['top_n'])]}
            for member in guild.members:
                if not self.included(member) or member == guild.me or member.top_role >= guild.me.top_role:
                    continue
                try:
                    match=conditions_match(rule['condition'],lambda c:self.resolve(guild,member,c,now))
                    if top_ids is not None:
                        match=match and str(member.id) in top_ids
                    has=role in member.roles
                    if match and not has:
                        await member.add_roles(role,reason='Pineapple Stats automatic role')
                        self.store.log(guild.id,self.user.id,'role_granted',{'member':str(member.id),'role':str(role.id)})
                        await self.notify(guild,member,role,rule,'earned')
                    elif not match and has and not rule.get('permanent',False):
                        await member.remove_roles(role,reason='Pineapple Stats conditions no longer met')
                        self.store.log(guild.id,self.user.id,'role_removed',{'member':str(member.id),'role':str(role.id)})
                        await self.notify(guild,member,role,rule,'lost')
                except Exception:
                    log.exception('Role rule %s failed for member %s',rule['id'],member.id)

    async def notify(self, guild, member, role, rule, action):
        message=f'🍍 {member.display_name} {action} the {role.name} role.'
        try:
            if rule.get('notify_dm'):
                await member.send(message)
            channel=guild.get_channel(int(rule.get('notification_channel') or 0))
            if channel:
                await channel.send(message)
        except discord.HTTPException:
            log.warning('Could not deliver role notification')

    async def counter_value(self, guild, counter):
        metric=counter.get('metric','members')
        now=time.time()
        if metric in ('members','humans','bots','online','roles','channels','boosts'):
            return {'members':guild.member_count or len(guild.members),
                    'humans':sum(not m.bot for m in guild.members),
                    'bots':sum(m.bot for m in guild.members),
                    'online':sum(m.status!=discord.Status.offline for m in guild.members),
                    'roles':len(guild.roles),'channels':len(guild.channels),
                    'boosts':guild.premium_subscription_count or 0}[metric]
        if metric=='clock':
            return datetime.now(ZoneInfo(counter.get('timezone') or self.store.settings(guild.id)['timezone'])).strftime(counter.get('format','%H:%M'))
        if metric=='youtube':
            key=os.getenv('YOUTUBE_API_KEY','')
            if not key:
                raise ValueError('Set YOUTUBE_API_KEY before enabling YouTube counters')
            channel_id=counter.get('youtube_channel','')
            cached=self.social_cache.get(channel_id)
            if cached and now-cached[0]<600:
                return cached[1]
            async with httpx.AsyncClient(timeout=20) as client:
                response=await client.get('https://www.googleapis.com/youtube/v3/channels',
                    params={'part':'statistics','id':channel_id,'key':key})
                response.raise_for_status()
                items=response.json().get('items',[])
                if not items:
                    raise ValueError('YouTube channel not found')
                value=int(items[0]['statistics'].get(counter.get('youtube_stat','subscriberCount'),0))
            self.social_cache[channel_id]=(now,value)
            return value
        if metric=='tiktok':
            # One account authorized by the operator; never arbitrary public-account scraping.
            token=os.getenv('TIKTOK_ACCESS_TOKEN','')
            if not token:
                raise ValueError('Set a TikTok access token with user.info.stats scope')
            cached=self.social_cache.get('tiktok')
            if cached and now-cached[0]<600:
                return cached[1]
            async with httpx.AsyncClient(timeout=20) as client:
                response=await client.get('https://open.tiktokapis.com/v2/user/info/',
                    params={'fields':'follower_count'},headers={'Authorization':'Bearer '+token})
                response.raise_for_status()
                payload=response.json()
                if payload.get('error',{}).get('code') not in (None,'ok'):
                    raise ValueError('TikTok authorization or API scope was rejected')
                value=int(payload['data']['user']['follower_count'])
            self.social_cache['tiktok']=(now,value)
            return value
        if metric=='role_members':
            role=guild.get_role(int(counter['role_id']))
            return len(role.members) if role else 0
        if metric=='invites':
            return sum(self.invite_total(guild,m.id,now-float(counter.get('days',14))*86400,now) for m in guild.members)
        data=self.store.summary(guild.id,now-float(counter.get('days',14))*86400,now,metric,
            members=counter.get('members'),channels=counter.get('channels'),
            roles=counter.get('roles'),activities=counter.get('activities'),apply_defaults=False)
        operation=counter.get('operation','total')
        if operation=='unique':
            return data['unique_members']
        if operation=='average':
            return round(data['total']/max(1,float(counter.get('days',14))),1)
        if operation=='top':
            values=data[counter.get('group','members')]
            return values[0]['name'] if values else 'Nobody'
        if operation=='change':
            days=float(counter.get('days',14))
            previous=self.store.summary(guild.id,now-days*172800,now-days*86400,metric,
                members=counter.get('members'),channels=counter.get('channels'),
                roles=counter.get('roles'),activities=counter.get('activities'),apply_defaults=False)
            return round(data['total']-previous['total'],1)
        return round(data['total'],1)

    async def update_counters(self, guild):
        # At most one worker per channel. A Discord retry sleeps only that worker.
        configured = self.store.objects(guild.id, 'counter')
        for counter in configured:
            if not counter.get('enabled', True):
                continue
            key = (guild.id, str(counter['channel_id']))
            task = self.counter_tasks.get(key)
            if task is None or task.done():
                self.counter_tasks[key] = asyncio.create_task(self.counter_worker(guild, counter['channel_id']))

    async def counter_worker(self, guild, channel_id):
        key = (guild.id, str(channel_id))
        while self.is_ready() and not self.is_closed():
            counter = next((c for c in self.store.objects(guild.id, 'counter')
                if str(c['channel_id']) == str(channel_id) and c.get('enabled', True)), None)
            channel = guild.get_channel(int(channel_id))
            if not counter or not channel:
                return
            mode = counter.get('update_mode', 'live')
            interval = max(5, int(counter.get('interval_seconds') or 5)) if mode == 'scheduled' else 2
            try:
                value = await self.counter_value(guild, counter)
                name = counter.get('template', '🍍 {value}').replace('{value}', str(value))[:100]
                self.counter_status[key] = {'id': counter['id'], 'value': value,
                    'desired_name': name, 'state': 'updating' if name != channel.name else 'current',
                    'checked_at': time.time(), 'published_name': channel.name}
                if name and name != channel.name:
                    await channel.edit(name=name, reason='Pineapple Stats live counter')
                    self.counter_status[key].update(state='current' if self.counter_status[key]['desired_name']==name else 'updating', published_name=name, published_at=time.time())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.counter_status[key] = {'id': counter['id'], 'state': 'error', 'error': type(exc).__name__, 'checked_at': time.time()}
                self.store.log(guild.id, self.user.id, 'counter_error', {'id': counter['id'], 'type': type(exc).__name__})
                interval = max(interval, 30)
            # No queued snapshots: the next cycle reads the newest config and value.
            await asyncio.sleep(interval)

    @tasks.loop(seconds=2)
    async def live_tick(self):
        if not self.is_ready():
            return
        self.flush()
        for guild in self.guilds:
            await self.update_counters(guild)
            # Web counters continue to show current values during a channel rename wait.
            for counter in self.store.objects(guild.id, 'counter'):
                key=(guild.id,str(counter['channel_id']))
                status=self.counter_status.get(key)
                if status and status.get('state')=='updating' and counter.get('enabled',True):
                    try:
                        value=await self.counter_value(guild,counter)
                        status.update(value=value,desired_name=counter.get('template','🍍 {value}').replace('{value}',str(value))[:100],checked_at=time.time())
                    except Exception:
                        pass

    async def close(self):
        self.live_tick.cancel()
        self.tick.cancel()
        pending = list(self.counter_tasks.values())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.counter_tasks.clear()
        await super().close()

    @tasks.loop(seconds=60)
    async def tick(self):
        if not self.is_ready():
            return
        self.flush()
        for guild in self.guilds:
            try:
                counts={s:sum(str(m.status)==s for m in guild.members) for s in ('online','idle','dnd','offline')}
                self.store.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?,?,?,?)',
                    (str(guild.id),int(time.time()//60)*60,guild.member_count or len(guild.members),
                     counts['online'],counts['idle'],counts['dnd'],counts['offline']))
                for member in guild.members:
                    self.reconcile(member)
                await self.apply_rules(guild)
            except Exception:
                log.exception('Scheduled work failed for guild %s',guild.id)

    async def sync_history(self, guild, channels, start, end):
        job_id=os.urandom(8).hex()
        job={'id':job_id,'guild':str(guild.id),'status':'running','scanned':0,'added':0,'errors':[]}
        self.jobs[job_id]=job
        async def run():
            try:
                for channel_id in channels:
                    channel=guild.get_channel_or_thread(int(channel_id))
                    if not channel or not hasattr(channel,'history'):
                        job['errors'].append(f'{channel_id}: not a text channel or accessible thread')
                        continue
                    try:
                        async for message in channel.history(limit=None,
                            after=datetime.fromtimestamp(start,timezone.utc),
                            before=datetime.fromtimestamp(end,timezone.utc),oldest_first=True):
                            job['scanned']+=1
                            if message.author.bot and not self.store.settings(guild.id)['include_bots']:
                                continue
                            parent=getattr(channel,'parent_id',None)
                            if isinstance(message.author,discord.Member):
                                self.remember_member(message.author)
                            job['added']+=self.store.message(message.id,guild.id,message.author.id,
                                parent or channel.id,message.created_at.timestamp())
                    except discord.HTTPException as exc:
                        job['errors'].append(f'{channel_id}: Discord HTTP {exc.status}')
                job['status']='completed_with_errors' if job['errors'] else 'completed'
            except Exception as exc:
                job['status']='failed'
                job['errors'].append(type(exc).__name__)
                log.exception('History sync failed')
            self.store.log(guild.id,self.user.id,'history_sync',job)
        asyncio.create_task(run())
        return job

    async def command_allowed(self, interaction):
        if not interaction.guild or not isinstance(interaction.user,discord.Member):
            await interaction.response.send_message('Use this command inside a server.',ephemeral=True)
            return False
        s=self.store.settings(interaction.guild_id)
        admin=interaction.user.guild_permissions.administrator or (s['elevate_manage_server'] and interaction.user.guild_permissions.manage_guild)
        if (s['command_access']=='admins' and not admin) or (s['command_channels'] and str(interaction.channel_id) not in s['command_channels'] and not admin):
            await interaction.response.send_message('You cannot use statistics commands here.',ephemeral=True)
            return False
        return True

    def register_commands(self):
        async def result(interaction, metric, days=14, member=None, channel=None, top=False):
            if not await self.command_allowed(interaction):
                return
            await interaction.response.defer(ephemeral=self.store.settings(interaction.guild_id)['ephemeral'])
            now=time.time()
            data=self.store.summary(interaction.guild_id,now-days*86400,now,metric,
                members=[str(member.id)] if member else None,channels=[str(channel.id)] if channel else None)
            embed=discord.Embed(title=f'🍍 {metric.title()} · {days} days',color=0xf3c644)
            embed.add_field(name='Total',value=f"{data['total']:,.1f} {data['unit']}")
            embed.add_field(name='Active members',value=str(data['unique_members']))
            if top:
                embed.description='\n'.join(f"**{i+1}.** {row['name']} — {row['value']:,.1f}" for i,row in enumerate(data['members'][:20])) or 'No recorded activity in this range.'
            await interaction.followup.send(embed=embed)

        @self.tree.command(name='stats',description='Server, member, or channel message and voice statistics')
        @app_commands.guild_only()
        @app_commands.choices(metric=[app_commands.Choice(name=k.title(),value=k) for k in ('messages','voice','activity','status','invites')])
        async def stats(interaction:discord.Interaction, metric:str='messages', days:app_commands.Range[int,1,36500]=14,
                        member:discord.Member=None,channel:discord.TextChannel=None):
            await result(interaction,metric,days,member,channel)

        @self.tree.command(name='top',description='Show the top twenty members; full list is available on the dashboard')
        @app_commands.guild_only()
        @app_commands.choices(metric=[app_commands.Choice(name=k.title(),value=k) for k in ('messages','voice','activity','invites')])
        async def top(interaction:discord.Interaction,metric:str='messages',days:app_commands.Range[int,1,36500]=14):
            await result(interaction,metric,days,top=True)

        @self.tree.command(name='dashboard',description='Open the Pineapple Stats web dashboard')
        @app_commands.guild_only()
        async def dashboard(interaction:discord.Interaction):
            if await self.command_allowed(interaction):
                await interaction.response.send_message(f"🍍 Dashboard: {os.getenv('PUBLIC_URL','http://localhost:8000')}",ephemeral=True)

        @self.tree.command(name='chart',description='Generate a message, voice, activity, or status chart')
        @app_commands.guild_only()
        @app_commands.choices(metric=[app_commands.Choice(name=k.title(),value=k) for k in ('messages','voice','activity','status','invites')])
        async def chart(interaction:discord.Interaction,metric:str='messages',days:app_commands.Range[int,1,36500]=14):
            if not await self.command_allowed(interaction):
                return
            await interaction.response.defer(ephemeral=self.store.settings(interaction.guild_id)['ephemeral'])
            data=self.store.summary(interaction.guild_id,time.time()-days*86400,time.time(),metric)
            def render():
                import matplotlib
                matplotlib.use('Agg')
                from matplotlib.figure import Figure
                fig=Figure(figsize=(9,4),facecolor='#121925')
                ax=fig.subplots()
                ax.set_facecolor('#121925')
                series=data['series']
                ax.plot([x['bucket'] for x in series],[x['value'] for x in series],color='#f3c644')
                ax.tick_params(colors='white',labelrotation=30)
                ax.set_title(f'{metric.title()} · {days} days',color='white')
                if len(series)>12:
                    ax.set_xticks(range(0,len(series),max(1,len(series)//8)))
                fig.tight_layout()
                output=io.BytesIO()
                fig.savefig(output,format='png')
                output.seek(0)
                return output
            output=await asyncio.to_thread(render)
            await interaction.followup.send(file=discord.File(output,filename='pineapple-stats.png'))

        @self.tree.command(name='ping',description='Check bot connection and Discord heartbeat latency')
        @app_commands.guild_only()
        async def ping(interaction:discord.Interaction):
            if not await self.command_allowed(interaction):
                return
            latency=self.latency
            embed=discord.Embed(title='🍍 Pong!',color=0xf3c644)
            embed.add_field(name='Bot status',value='🟢 Connected' if self.is_ready() else '🟡 Connecting')
            embed.add_field(name='Discord heartbeat latency',value=f'{latency*1000:.0f} ms' if math.isfinite(latency) else 'Measuring…')
            embed.add_field(name='Slash commands',value='Synced' if self.synced else 'Syncing…')
            await interaction.response.send_message(embed=embed,ephemeral=True)

        @self.tree.command(name='help',description='Show Pineapple Stats commands')
        @app_commands.guild_only()
        async def help_command(interaction:discord.Interaction):
            await interaction.response.send_message('🍍 **Pineapple Stats**\n`/stats` — messages, voice, activity, status, invites\n`/top` — member leaderboard\n`/chart` — graph image\n`/dashboard` — analytics and settings\n`/ping` — bot status and Discord latency\nAdministrators manage automatic roles, counters, history imports, presets, API keys and permissions on the dashboard.',ephemeral=True)

        @self.tree.error
        async def command_error(interaction,error):
            log.error('Slash command failed: %s',type(error).__name__)
            send=interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            await send('That command failed. Check the bot hosting logs and try again.',ephemeral=True)
