"""SQLite analytics store. No message text, attachments, or access tokens are stored."""
import json
import sqlite3
import time
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DEFAULTS = {
    'timezone': 'America/Los_Angeles', 'lookback_days': 1, 'interval': 'half_hour',
    'activity_tracking': True, 'include_bots': False, 'track_voice_alone': True,
    'track_muted': True, 'track_deafened': True, 'command_access': 'members',
    'command_channels': [], 'dashboard_access': 'admins', 'viewer_roles': [],
    'editor_roles': [], 'elevate_manage_server': True, 'excluded_channels': [],
    'excluded_members': [], 'role_filter': [], 'filter_mode': 'exclude',
    'invite_min_account_days': 7, 'invite_min_join_days': 1, 'invite_required_roles': [],
    'ephemeral': True, 'aggregate_channels': {}, 'notification_channel': None,
}

class Store:
    def __init__(self, path):
        if path != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          PRAGMA foreign_keys=ON;
          CREATE TABLE IF NOT EXISTS config(guild TEXT PRIMARY KEY, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, guild TEXT, member TEXT, channel TEXT, ts REAL);
          CREATE INDEX IF NOT EXISTS message_query ON messages(guild,ts,member,channel);
          CREATE TABLE IF NOT EXISTS spans(id INTEGER PRIMARY KEY, guild TEXT, member TEXT, channel TEXT, kind TEXT, label TEXT, start REAL, end REAL, seconds REAL);
          CREATE INDEX IF NOT EXISTS span_query ON spans(guild,kind,start,end);
          CREATE TABLE IF NOT EXISTS members(guild TEXT, id TEXT, name TEXT, roles TEXT, joined REAL, created REAL, present INTEGER, PRIMARY KEY(guild,id));
          CREATE TABLE IF NOT EXISTS channels(guild TEXT, id TEXT, name TEXT, PRIMARY KEY(guild,id));
          CREATE TABLE IF NOT EXISTS snapshots(guild TEXT, ts REAL, members INTEGER, online INTEGER, idle INTEGER, dnd INTEGER, offline INTEGER, PRIMARY KEY(guild,ts));
          CREATE TABLE IF NOT EXISTS invites(guild TEXT, member TEXT, code TEXT, inviter TEXT, ts REAL, left_at REAL, reason TEXT, PRIMARY KEY(guild,member,ts));
          CREATE TABLE IF NOT EXISTS objects(id INTEGER PRIMARY KEY, guild TEXT, kind TEXT, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, guild TEXT, actor TEXT, ts REAL, action TEXT, details TEXT);
          CREATE TABLE IF NOT EXISTS keys(hash TEXT PRIMARY KEY, guild TEXT, created REAL);
        ''')
        self.db.commit()

    def execute(self, sql, args=()):
        cur = self.db.execute(sql, args)
        self.db.commit()
        return cur

    def rows(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args)]

    def settings(self, guild):
        row = self.db.execute('SELECT data FROM config WHERE guild=?', (str(guild),)).fetchone()
        return {**DEFAULTS, **(json.loads(row[0]) if row else {})}

    def save_settings(self, guild, data):
        merged = {**self.settings(guild), **data}
        ZoneInfo(merged['timezone'])
        self.execute('INSERT OR REPLACE INTO config VALUES(?,?)', (str(guild), json.dumps(merged)))

    def log(self, guild, actor, action, details):
        self.execute('INSERT INTO audit(guild,actor,ts,action,details) VALUES(?,?,?,?,?)',
                     (str(guild), str(actor), time.time(), action, json.dumps(details)))

    def message(self, mid, guild, member, channel, ts):
        return self.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?)',
                            tuple(map(str, (mid, guild, member, channel))) + (ts,)).rowcount

    def span(self, guild, member, channel, kind, label, start, end):
        if end <= start:
            return
        self.execute('INSERT INTO spans(guild,member,channel,kind,label,start,end,seconds) VALUES(?,?,?,?,?,?,?,?)',
                     (str(guild), str(member), str(channel), kind, label, start, end, end-start))

    def object(self, guild, kind, data, oid=None):
        if oid is None:
            return self.execute('INSERT INTO objects(guild,kind,data) VALUES(?,?,?)',
                                (str(guild), kind, json.dumps(data))).lastrowid
        self.execute('UPDATE objects SET data=? WHERE id=? AND guild=? AND kind=?',
                     (json.dumps(data), oid, str(guild), kind))
        return oid

    def objects(self, guild, kind):
        return [{'id': r['id'], **json.loads(r['data'])} for r in
                self.rows('SELECT id,data FROM objects WHERE guild=? AND kind=?', (str(guild), kind))]

    def member(self, guild, mid, name, roles, joined, created, present=True):
        self.execute('INSERT OR REPLACE INTO members VALUES(?,?,?,?,?,?,?)',
                     (str(guild), str(mid), name, json.dumps(list(map(str, roles))), joined, created, int(present)))

    def summary(self, guild, start, end, metric='messages', interval='day', members=None,
                channels=None, roles=None, activities=None, apply_defaults=True, selection_mode="include", time_unit="minutes"):
        if selection_mode not in ("include", "exclude") or time_unit not in ("minutes", "hours", "days"):
            raise ValueError("Invalid selection mode or time unit")
        if end <= start or metric not in ('messages','voice','activity','status','invites'):
            raise ValueError('Invalid date range or metric')
        if interval not in ('half_hour','hour','day','week','month'):
            raise ValueError('Invalid interval')
        if interval == 'half_hour' and (end-start)/1800 > 10000:
            raise ValueError('Choose a shorter range for half-hour intervals (maximum 208 days)')
        guild = str(guild)
        settings = self.settings(guild)
        tz = ZoneInfo(settings['timezone'])
        directory = {r['id']: r for r in self.rows('SELECT * FROM members WHERE guild=?', (guild,))}
        channel_names = {r['id']: r['name'] for r in self.rows('SELECT * FROM channels WHERE guild=?', (guild,))}
        excluded_m = set(settings['excluded_members']) if apply_defaults else set()
        excluded_c = set(settings['excluded_channels']) if apply_defaults else set()
        roles = set(roles or [])
        default_roles = set(settings['role_filter']) if apply_defaults else set()
        include = settings['filter_mode'] == 'include'
        member_ids = set(members or [])
        channel_ids = set(channels or [])
        activity_names = set(activities or [])
        aggregate = settings['aggregate_channels']
        mapped = {source: target for target, sources in aggregate.items() for source in sources}

        def matches(selected, found):
            return not selected or (found == (selection_mode == "include"))

        def allowed(member, channel, label):
            current_roles = set(json.loads(directory.get(member, {}).get('roles', '[]')))
            return (member not in excluded_m and channel not in excluded_c
                    and matches(member_ids, member in member_ids)
                    and matches(channel_ids, channel in channel_ids or mapped.get(channel) in channel_ids)
                    and matches(roles, bool(current_roles & roles))
                    and (not default_roles or (bool(current_roles & default_roles) == include))
                    and matches(activity_names, label in activity_names))

        series, heatmap, by_member, by_channel, by_label = {}, {}, {}, {}, {}
        users, total = set(), 0
        bucket_users = {}
        divisor = {"minutes":1,"hours":60,"days":1440}[time_unit] if metric in ("voice","activity","status") else 1
        def add(member, channel, label, ts, amount):
            nonlocal total
            if not allowed(member, channel, label):
                return
            amount /= divisor
            channel = mapped.get(channel, channel)
            dt = datetime.fromtimestamp(ts, tz)
            if interval == 'half_hour':
                bucket = datetime.fromtimestamp(int(ts)//1800*1800,tz).isoformat()
            elif interval == 'hour':
                # Include offset to distinguish repeated DST hours.
                bucket = dt.replace(minute=0, second=0, microsecond=0).isoformat()
            elif interval == 'week':
                bucket = f'{dt.isocalendar().year}-W{dt.isocalendar().week:02}'
            elif interval == 'month':
                bucket = dt.strftime('%Y-%m')
            else:
                bucket = dt.strftime('%Y-%m-%d')
            key = f'{dt.weekday()}:{dt.hour}'
            series[bucket] = series.get(bucket, 0) + amount
            heatmap[key] = heatmap.get(key, 0) + amount
            by_member[member] = by_member.get(member, 0) + amount
            by_channel[channel] = by_channel.get(channel, 0) + amount
            by_label[label] = by_label.get(label, 0) + amount
            users.add(member)
            bucket_users.setdefault(bucket, set()).add(member)
            total += amount

        if metric == 'messages':
            for r in self.db.execute('SELECT member,channel,ts FROM messages WHERE guild=? AND ts>=? AND ts<?', (guild,start,end)):
                add(r['member'], r['channel'], '', r['ts'], 1)
        elif metric == 'invites':
            for r in self.db.execute('SELECT * FROM invites WHERE guild=? AND ts>=? AND ts<?', (guild,start,end)):
                add(r['inviter'] or 'unknown', r['code'] or 'unknown', r['reason'] or 'valid', r['ts'], 1)
        else:
            for r in self.db.execute('SELECT * FROM spans WHERE guild=? AND kind=? AND end>? AND start<?', (guild,metric,start,end)):
                a, b = max(start,r['start']), min(end,r['end'])
                # Split at UTC hour boundaries, which align with local hours in most timezones.
                # Also split every minute for fractional-offset timezones.
                step = 1800 if interval == 'half_hour' else 60 if datetime.fromtimestamp(a,tz).utcoffset().total_seconds() % 3600 else 3600
                while a < b:
                    stop = min(b, (int(a)//step+1)*step)
                    add(r['member'],r['channel'],r['label'],a,(stop-a)/60)
                    a = stop

        if interval == 'half_hour':
            ts=int(start)//1800*1800
            while ts < end:
                bucket=datetime.fromtimestamp(ts,tz).isoformat()
                series.setdefault(bucket,0)
                bucket_users.setdefault(bucket,set())
                ts+=1800

        def ranked(values, names):
            return [{'id': k, 'name': names.get(k,k), 'value': round(v,3)} for k,v in
                    sorted(values.items(), key=lambda item: (-item[1],item[0]))]
        return {'metric': metric, 'unit': time_unit if metric in ('voice','activity','status') else 'count',
                'total': round(total,3), 'unique_members': len(users), 'start': start, 'end': end,
                'series': [{'bucket':k,'value':round(v,3)} for k,v in sorted(series.items(),key=lambda item: datetime.fromisoformat(item[0]).timestamp() if interval == 'half_hour' else item[0])],
                'unique_series': [{'bucket': k, 'value': len(v)} for k,v in sorted(bucket_users.items(),key=lambda item: datetime.fromisoformat(item[0]).timestamp() if interval == 'half_hour' else item[0])],
                'heatmap': heatmap,
                'members': ranked(by_member,{k:v['name'] for k,v in directory.items()}),
                'channels': ranked(by_channel,channel_names), 'labels': ranked(by_label,{})}

    def close(self):
        self.db.close()
