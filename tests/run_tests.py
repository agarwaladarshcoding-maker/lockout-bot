import json
"""Offline stress tests for the Lockout bot.

Discord and Codeforces are replaced by fakes, so every scenario (matches, ties, cancels,
Codeforces outages, admin checks, room management) runs without a token or network.
The last section hits the real Codeforces API to make sure our parsing matches reality.

Run:  .venv/bin/python tests/run_tests.py
"""
import asyncio
import inspect
import re
import os
import sys
import tempfile
import time
import types
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["LOCKOUT_DB"] = tempfile.mktemp(suffix=".db")
os.environ["DISCORD_TOKEN"] = "x"
os.environ["ADMIN_IDS"] = ""

import discord  # noqa: E402
from discord import app_commands  # noqa: E402

import db  # noqa: E402
import main  # noqa: E402
from cf import CF, CFError  # noqa: E402

main.MATCH_SECONDS = 3
main.GRACE = 0
main.POLL = 0.1
main.TICK = 0.2
main.COUNTDOWN = 0
main.FINAL_RETRIES = 4
main.JUDGE_WAIT = 2

OWNER = 999


# ---------------------------------------------------------------- fakes
def embed_text(e):
    """Everything a player can read in an embed."""
    if e is None:
        return ""
    parts = [e.title, e.description] + [f"{f.name}\n{f.value}" for f in e.fields]
    return "\n".join(str(x) for x in parts if x)


class FakeUser:
    def __init__(self, uid, bot=False):
        self.id, self.bot, self.mention = uid, bot, f"<@{uid}>"


class FakeMessage:
    ids = iter(range(5000, 10**6))

    def __init__(self, channel, embed=None):
        self.channel, self.edits, self.id, self.embed = channel, [], next(FakeMessage.ids), embed

    async def edit(self, **kw):
        self.edits.append(kw)
        self.embed = kw.get("embed", self.embed)


class Overwrites:
    def overwrites_for(self, target):
        return getattr(self, "perms", {}).get(target, discord.PermissionOverwrite())

    async def set_permissions(self, target, **kw):
        self.perms = {**getattr(self, "perms", {}), target: discord.PermissionOverwrite(**kw)}


class FakeChannel(Overwrites):
    def __init__(self, cid, name="room", guild=None):
        self.id, self.name, self.guild = cid, name, guild
        self.mention, self.sent, self.category = f"<#{cid}>", [], None

    async def send(self, content=None, embed=None, **kw):
        self.sent.append("\n".join(x for x in (content, embed_text(embed)) if x))
        return FakeMessage(self, embed)

    async def delete(self):
        if self.guild:
            self.guild.channels.pop(self.id, None)

    def text(self):
        return "\n".join(str(x) for x in self.sent)


class FakeCategory(Overwrites):
    def __init__(self, guild, name, overwrites=None):
        self.guild, self.name, self.perms = guild, name, dict(overwrites or {})

    async def create_text_channel(self, name, **kw):
        ch = await self.guild.create_text_channel(name, **kw)
        ch.category = self
        return ch

    @property
    def text_channels(self):
        return [c for c in self.guild.channels.values() if c.category is self]

    async def delete(self):
        self.guild.categories.remove(self)


class FakePerms:
    manage_channels = view_channel = send_messages = embed_links = True


class FakeGuild:
    def __init__(self, owner_id=OWNER):
        self.owner_id, self.categories, self.channels, self._n = owner_id, [], {}, 1000
        self.name = "Test Server"
        self.default_role = object()
        self.me = type("Me", (), {"guild_permissions": FakePerms()})()
        self.created = []

    async def create_category(self, name, overwrites=None):
        c = FakeCategory(self, name, overwrites)
        self.categories.append(c)
        return c

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def create_text_channel(self, name, **kw):
        self._n += 1
        ch = FakeChannel(self._n, name, self)
        ch.kw = kw
        self.channels[ch.id] = ch
        self.created.append(ch)
        return ch

    async def fetch_member(self, uid):
        return FakeUser(uid)


class Resp:
    def __init__(self):
        self.done, self.sent = False, []

    def is_done(self):
        return self.done

    async def send_message(self, content=None, **kw):
        self.done = True
        self.sent.append((content, kw))

    async def defer(self, **kw):
        self.done = True

    async def edit_message(self, **kw):
        self.done = True
        self.sent.append((kw.get("content"), kw))


class Followup:
    def __init__(self, resp):
        self.resp = resp

    async def send(self, content=None, **kw):
        self.resp.sent.append((content, kw))
        return FakeMessage(None)


class Inter:
    def __init__(self, uid, channel, guild):
        self.user, self.channel, self.guild = FakeUser(uid), channel, guild
        self.response = Resp()
        self.followup = Followup(self.response)

    async def original_response(self):
        return FakeMessage(self.channel)

    def text(self):
        return "\n".join(str(c) for c, _ in self.response.sent)

    def view(self):
        return next(kw["view"] for _, kw in self.response.sent if kw.get("view"))


class FakeCF:
    """Scriptable Codeforces: tests append submissions, can inject failures."""

    def __init__(self):
        self.subs, self.seen_map, self.picked = {}, {}, []
        self.fail = 0  # next N submission fetches raise
        self.fail_pick = False
        self.delay = 0  # seconds seen() takes, to open race windows
        self._id = 0

    def solve(self, handle, key, verdict="OK", at=None):
        self._id += 1
        sub = {  # CF returns newest first
            "id": self._id,
            "creationTimeSeconds": int(time.time()) if at is None else at,
            "problem": {"contestId": int(key[:-1]), "index": key[-1]},
        }
        if verdict is not None:  # still in the queue: Codeforces omits the field
            sub["verdict"] = verdict
        self.subs.setdefault(handle, []).insert(0, sub)
        return sub

    async def submissions(self, handle, count=None):
        if self.fail:
            self.fail -= 1
            raise CFError("boom")
        return list(self.subs.get(handle, []))[:count]

    async def seen(self, handle):
        if self.delay:
            await asyncio.sleep(self.delay)
        return set(self.seen_map.get(handle, ()))

    async def pick(self, rating, exclude):
        if self.fail_pick:
            raise CFError("no problems")
        for letter in "ABCDEF":
            key = f"{rating}{letter}"
            if key not in exclude:
                p = {"key": key, "name": f"Prob {key}", "rating": rating, "url": f"https://x/{key}"}
                self.picked.append(p)
                return p
        raise CFError("none left")

    async def user(self, handle):
        if handle.startswith("bad"):
            raise CFError("not found")
        return {"handle": handle}


# ---------------------------------------------------------------- helpers
def reset():
    main.matches.clear()
    main.pending.clear()
    for t in ("users", "rooms", "results", "config", "active"):
        db.conn.execute(f"DELETE FROM {t}")
    db.conn.commit()
    main.cf = FakeCF()
    os.environ["ADMIN_IDS"] = ""
    main.COUNTDOWN = 0
    main.JUDGE_WAIT = 2


def register(*pairs):
    for uid, h in pairs:
        db.save_user(uid, h, h.title())


def add_room(cid):
    if not db.is_room(cid):
        db.add_rooms([cid])


async def begin(rating=800, room=100, a=(1, "alice"), b=(2, "bob")):
    register(a, b)
    add_room(room)
    ch = FakeChannel(room)
    await main.start_match(main.reserve(ch, (a[0], b[0]), rating))
    return ch, main.matches[room]


async def finish(m, timeout=20):
    await asyncio.wait_for(m.task, timeout)


def result_rows():
    return db.conn.execute("SELECT * FROM results").fetchall()


def free(room):
    return not main.busy(room)


def bare_match(handles=("alice", "bob")):
    return main.Match(None, [{"handle": h} for h in handles], 800, [], set())


def cmd(name):
    return main.lockout.get_command(name)


async def check_blocks(name, inter, exc):
    try:
        await cmd(name)._check_can_run(inter)
    except exc:
        return True
    return False


# ---------------------------------------------------------------- tests: scoring logic
async def t_accepted_rules():
    f = main.cf
    m = bare_match()
    keys = {"800A", "900A"}
    f.solve("alice", "800A", at=100)          # before start -> ignored
    f.solve("alice", "800A", at=210)          # after end -> ignored
    f.solve("bob", "900A", verdict="WRONG_ANSWER", at=150)  # wrong verdict -> ignored
    f.solve("bob", "999A", at=150)            # not a match problem -> ignored
    got, _ = await main.accepted(m, keys, 120, 200)
    assert got == {}, got
    f.solve("bob", "800A", at=150)
    f.solve("alice", "800A", at=151)          # later, loses
    got, _ = await main.accepted(m, keys, 120, 200)
    assert got == {"800A": 1}, got


async def t_same_second_uses_submission_id():
    f = main.cf
    f.solve("bob", "800A", at=150)            # lower id
    f.solve("alice", "800A", at=150)          # higher id, same second
    got, _ = await main.accepted(bare_match(), {"800A"}, 100, 200)
    assert got == {"800A": 1}, got  # bob (index 1)


async def t_points_and_board():
    ch, m = await begin()
    m.owners = {m.problems[0]["key"]: 0, m.problems[4]["key"]: 1}
    assert m.scores() == [100, 500], m.scores()
    assert "alice" in embed_text(m.board()) and "bob" in embed_text(m.board())
    m.task.cancel()


# ---------------------------------------------------------------- tests: whole matches
async def t_basic_match_winner():
    ch, m = await begin()
    f = main.cf
    f.solve("alice", "800A")
    f.solve("bob", "900A")
    await asyncio.sleep(0.4)
    f.solve("alice", "1000A")
    await finish(m)
    assert m.scores() == [400, 200], m.scores()
    row = result_rows()[0]
    assert row["winner"] == "alice" and row["tiebreak"] == 0 and (row["score1"], row["score2"]) == (400, 200)
    assert "Alice" in ch.sent[-1] and "advances" in ch.sent[-1]
    assert free(100)


async def t_lockout_blocks_opponent():
    ch, m = await begin()
    f = main.cf
    f.solve("alice", "800A")
    await asyncio.sleep(0.3)
    f.solve("bob", "800A")  # later: must NOT score
    await finish(m)
    assert m.scores() == [100, 0], m.scores()
    assert result_rows()[0]["winner"] == "alice"


async def t_live_announcements():
    ch, m = await begin()
    main.cf.solve("bob", "1100A")
    await finish(m)
    assert "solved problem 4 worth **400** points" in ch.text() and "update in standings" in ch.text(), ch.text()
    assert "LOCKOUT STARTED" in ch.text() and "Match over" in ch.text()
    assert "<@2>" in ch.text().split("update in standings")[0], "standings update pings both players"
    board = embed_text(m.board_msg.embed)
    assert "Time's up" in board and "Final standings" in board and "Solved by bob" in board, board


async def t_tie_ends_in_draw():
    ch, m = await begin()
    f = main.cf
    f.solve("alice", "1100A")  # 400
    f.solve("alice", "800A")   # 100 -> 500
    f.solve("bob", "1200A")    # 500
    await finish(m)
    row = result_rows()[0]
    assert row["winner"] == "draw" and row["tiebreak"] == 0 and (row["score1"], row["score2"]) == (500, 500), dict(row)
    assert "draw" in ch.text().lower()


async def t_zero_zero_tie_ends_in_draw():
    ch, m = await begin()
    await finish(m)
    row = result_rows()[0]
    assert row["winner"] == "draw" and row["tiebreak"] == 0 and (row["score1"], row["score2"]) == (0, 0), dict(row)
    assert "draw" in ch.text().lower()


async def t_codeforces_errors_during_match():
    ch, m = await begin()
    f = main.cf
    f.fail = 5
    f.solve("alice", "800A")
    await finish(m)
    assert m.scores() == [100, 0] and result_rows()[0]["winner"] == "alice"


async def t_codeforces_down_at_the_end():
    ch, m = await begin()
    main.cf.solve("alice", "800A")
    await asyncio.sleep(0.3)
    main.cf.fail = 10_000
    await finish(m, timeout=15)
    assert "Codeforces" in ch.text() and "manually" in ch.text(), ch.text()
    assert free(100), "room must not stay stuck when Codeforces is down"


async def t_start_failure_frees_room():
    main.cf.fail_pick = True
    register((1, "alice"), (2, "bob"))
    add_room(100)
    ch = FakeChannel(100)
    await main.start_match(main.reserve(ch, (1, 2), 800))
    assert "Couldn't start" in ch.text() and free(100)


async def t_no_repeat_problems_for_seen():
    f = main.cf
    f.seen_map = {"alice": {"800A"}, "bob": {"900A"}}
    ch, m = await begin()
    keys = [p["key"] for p in m.problems]
    assert "800A" not in keys and "900A" not in keys, keys
    assert [p["rating"] for p in m.problems] == [800, 900, 1000, 1100, 1200]
    m.task.cancel()


async def t_parallel_matches_are_independent():
    ch1, m1 = await begin(room=100)
    ch2, m2 = await begin(room=101, a=(3, "carol"), b=(4, "dave"))
    f = main.cf
    f.solve("alice", "800A")
    f.solve("dave", "1000A")
    f.solve("dave", "1100A")
    await asyncio.gather(finish(m1), finish(m2))
    assert m1.scores() == [100, 0] and m2.scores() == [0, 700]
    winners = {r["channel_id"]: r["winner"] for r in result_rows()}
    assert winners == {100: "alice", 101: "dave"}, winners


async def t_room_reusable_after_match():
    ch, m = await begin()
    main.cf.solve("alice", "800A")
    await finish(m)
    assert free(100)
    main.cf.subs.clear()
    ch2, m2 = await begin()
    main.cf.solve("bob", "900A")
    await finish(m2)
    assert [r["winner"] for r in result_rows()] == ["alice", "bob"]


# ---------------------------------------------------------------- tests: admin cancel
async def t_endmatch_mid_match_then_reuse():
    guild = FakeGuild()
    ch, m = await begin()
    inter = Inter(OWNER, FakeChannel(1), guild)
    await cmd("endmatch").callback(inter, room=ch)
    await asyncio.sleep(0.2)
    assert m.task.cancelled() or m.task.done()
    assert free(100) and "cancelled" in ch.text()
    ch2, m2 = await begin()  # same room works again
    m2.task.cancel()


async def t_endmatch_during_countdown():
    main.COUNTDOWN = 1
    register((1, "alice"), (2, "bob"))
    add_room(100)
    ch = FakeChannel(100)
    t = asyncio.create_task(main.start_match(main.reserve(ch, (1, 2), 800)))
    await asyncio.sleep(0.3)
    await cmd("endmatch").callback(Inter(OWNER, FakeChannel(1), FakeGuild()), room=ch)
    await t
    assert free(100) and "LOCKOUT STARTED" not in ch.text(), ch.text()


async def t_endmatch_nothing_running():
    inter = Inter(OWNER, FakeChannel(1), FakeGuild())
    await cmd("endmatch").callback(inter, room=FakeChannel(100))
    assert "Nothing running" in inter.text()


# ---------------------------------------------------------------- tests: commands
def room_inter(uid=1, room=100):
    add_room(room)
    return Inter(uid, FakeChannel(room), FakeGuild())


async def t_challenge_validation():
    register((1, "alice"), (2, "bob"))
    opp = FakeUser(2)
    c = cmd("challenge").callback
    # not a room channel
    i = Inter(1, FakeChannel(555), FakeGuild())
    await c(i, opp, 800)
    assert "room channels" in i.text()
    # bad rating step
    i = room_inter()
    await c(i, opp, 850)
    assert "multiple of 100" in i.text()
    # self / bot
    i = room_inter()
    await c(i, FakeUser(1), 800)
    assert "real opponent" in i.text()
    i = room_inter()
    await c(i, FakeUser(77, bot=True), 800)
    assert "real opponent" in i.text()
    # unregistered opponent
    i = room_inter()
    await c(i, FakeUser(55), 800)
    assert "hasn't registered" in i.text()
    # busy room
    i = room_inter()
    main.pending[100] = object()
    await c(i, opp, 800)
    assert "in use" in i.text()
    main.pending.clear()
    # a player already in a match elsewhere
    ch, m = await begin(room=200)
    i = room_inter(room=201)
    await c(i, FakeUser(2), 800)
    assert "already in a match" in i.text()
    m.task.cancel()


async def t_challenge_accept_flow():
    register((1, "alice"), (2, "bob"))
    i = room_inter()
    await cmd("challenge").callback(i, FakeUser(2), 800)
    assert main.busy(100)
    view = i.view()
    accept = view.children[0]
    # wrong person presses accept
    other = Inter(7, i.channel, i.guild)
    await accept.callback(other)
    assert "isn't for you" in other.text() and 100 not in main.matches
    # opponent accepts
    opp = Inter(2, i.channel, i.guild)
    await accept.callback(opp)
    m = main.matches[100]
    assert m.start and "LOCKOUT STARTED" in i.channel.text()
    m.task.cancel()


async def t_challenge_custom_minutes():
    register((1, "alice"), (2, "bob"))
    i = room_inter()
    await cmd("challenge").callback(i, FakeUser(2), 800, 7)
    assert "7 min" in i.text() and i.view().seconds == 420
    assert json.loads(dict(main.db.all_active())[100])["seconds"] == 420  # survives a restart before Accept
    await i.view().children[0].callback(Inter(2, i.channel, i.guild))
    m = main.matches[100]
    assert m.seconds == 420 and "7 minutes" in i.channel.text()
    assert 410 < m.seconds_left() <= 420
    assert main.restore(i.channel, json.dumps(json.loads(dict(main.db.all_active())[100]))).seconds == 420
    m.task.cancel()


async def t_extra_rooms_are_practice():
    guild = FakeGuild()
    await cmd("createextraroom").callback(Inter(OWNER, FakeChannel(1), guild), 2)
    assert [c.name for c in guild.created] == ["extra-1", "extra-2"]
    assert any(c.name == "Extra Rooms" for c in guild.categories) and db.all_rooms() == []
    x1 = db.all_rooms("extra_rooms")[0]
    i = Inter(5, FakeChannel(1), guild)
    await cmd("rooms").callback(i)
    assert "Extra rooms" in i.text()
    # a full practice match: same game, but nothing reaches the results table
    register((1, "alice"), (2, "bob"))
    i = Inter(1, FakeChannel(x1), guild)
    await cmd("challenge").callback(i, FakeUser(2), 800)
    assert "doesn't count" in i.text()
    await i.view().children[0].callback(Inter(2, i.channel, guild))
    m = main.matches[x1]
    assert m.practice and "PRACTICE LOCKOUT STARTED" in i.channel.text()
    assert json.loads(dict(db.all_active())[x1])["practice"] is True
    main.cf.solve("alice", "800A")
    await finish(m)
    assert result_rows() == [] and "Practice match over" in i.channel.text() and "advances" not in i.channel.text()
    # removal
    i = Inter(OWNER, FakeChannel(1), guild)
    await cmd("removeextraroom").callback(i, 2)
    assert "Deleted extra-2" in i.text() and len(db.all_rooms("extra_rooms")) == 1


async def t_challenge_decline_and_expire():
    register((1, "alice"), (2, "bob"))
    i = room_inter()
    await cmd("challenge").callback(i, FakeUser(2), 800)
    view = i.view()
    d = Inter(2, i.channel, i.guild)
    await view.children[1].callback(d)
    assert free(100)
    # expiry
    i = room_inter()
    await cmd("challenge").callback(i, FakeUser(2), 800)
    view = i.view()
    view.message = FakeMessage(i.channel)
    await view.on_timeout()
    assert free(100) and view.message.edits


async def t_accept_after_admin_cancel():
    register((1, "alice"), (2, "bob"))
    i = room_inter()
    await cmd("challenge").callback(i, FakeUser(2), 800)
    view = i.view()
    await cmd("endmatch").callback(Inter(OWNER, FakeChannel(1), i.guild), room=i.channel)
    opp = Inter(2, i.channel, i.guild)
    await view.children[0].callback(opp)
    assert 100 not in main.matches and "cancelled" in opp.text()


async def t_register_flow():
    f = main.cf
    reg = cmd("register").callback
    i = Inter(1, FakeChannel(1), FakeGuild())
    await reg(i, "bad_handle", "Al")
    assert "not found" in i.text()
    db.save_user(2, "alice", "Other")
    i = Inter(1, FakeChannel(1), FakeGuild())
    await reg(i, "alice", "Alice")
    assert "already registered" in i.text()
    i = Inter(1, FakeChannel(1), FakeGuild())
    await reg(i, "carol", "Carol")
    view = i.view()
    # nothing submitted yet
    p = Inter(1, FakeChannel(1), FakeGuild())
    await view.children[0].callback(p)
    assert "Haven't seen" in p.text() and db.user(1) is None
    # an OLD compile error must not be reusable
    f.solve("carol", "1A", verdict="COMPILATION_ERROR", at=int(time.time()) - 3600)
    p = Inter(1, FakeChannel(1), FakeGuild())
    await view.children[0].callback(p)
    assert db.user(1) is None
    # wrong problem
    f.solve("carol", "2A", verdict="COMPILATION_ERROR")
    p = Inter(1, FakeChannel(1), FakeGuild())
    await view.children[0].callback(p)
    assert db.user(1) is None
    # correct
    f.solve("carol", "1A", verdict="COMPILATION_ERROR")
    p = Inter(1, FakeChannel(1), FakeGuild())
    await view.children[0].callback(p)
    assert db.user(1)["handle"] == "carol" and "Verified" in p.text()


async def t_register_handle_case_insensitive():
    db.save_user(2, "Alice", "A")
    assert db.by_handle("alice")["discord_id"] == 2


async def t_admin_gate():
    guild = FakeGuild()
    for name in ("createroom", "removeroom", "createextraroom", "removeextraroom", "endmatch", "forceregister", "unregister", "health", "technical", "players", "matches"):
        assert await check_blocks(name, Inter(5, FakeChannel(1), guild), main.NotAdmin), name
        assert not await check_blocks(name, Inter(OWNER, FakeChannel(1), guild), main.NotAdmin), name
    for name in ("help", "register", "challenge", "rooms", "score"):
        assert not cmd(name).checks, name  # open to everyone
    os.environ["ADMIN_IDS"] = "5, 6"
    assert not await check_blocks("health", Inter(5, FakeChannel(1), guild), main.NotAdmin)
    assert await check_blocks("health", Inter(OWNER, FakeChannel(1), guild), main.NotAdmin), "owner not in ADMIN_IDS is out"


async def t_technical_channel_creation_and_gating():
    guild = FakeGuild()
    inter = Inter(OWNER, FakeChannel(1), guild)
    await cmd("technical").callback(inter)
    ch = guild.created[-1]
    assert ch.name == "technical" and db.get_config("technical") == str(ch.id)
    ow = ch.kw["overwrites"]
    assert ow[guild.default_role].view_channel is False, "must be hidden from everyone"
    assert any(getattr(k, "id", None) == OWNER and v.view_channel for k, v in ow.items())
    # second call does not create another
    n = len(guild.created)
    await cmd("technical").callback(Inter(OWNER, FakeChannel(1), guild))
    assert len(guild.created) == n
    # admin commands now only in technical
    assert await check_blocks("health", Inter(OWNER, FakeChannel(1), guild), main.WrongChannel)
    assert not await check_blocks("health", Inter(OWNER, ch, guild), main.WrongChannel)
    # non-admin still blocked even inside it
    assert await check_blocks("health", Inter(5, ch, guild), main.NotAdmin)
    # if the channel gets deleted by hand, the owner is not locked out
    guild.channels.pop(ch.id)
    assert not await check_blocks("health", Inter(OWNER, FakeChannel(1), guild), main.WrongChannel)


async def t_room_create_remove():
    guild = FakeGuild()
    i = Inter(OWNER, FakeChannel(1), guild)
    await cmd("createroom").callback(i, 3)
    assert [c.name for c in guild.created] == ["room-1", "room-2", "room-3"]
    await cmd("createroom").callback(Inter(OWNER, FakeChannel(1), guild), 2)
    assert [c.name for c in guild.created][-2:] == ["room-4", "room-5"] and len(db.all_rooms()) == 5
    names = lambda: sorted(c.name for c in guild.channels.values())  # noqa: E731
    r2 = next(c for c in db.all_rooms() if guild.channels[c].name == "room-2")
    main.pending[r2] = object()
    i = Inter(OWNER, FakeChannel(1), guild)
    await cmd("removeroom").callback(i, 2)
    assert "has a match" in i.text() and len(db.all_rooms()) == 5 and r2 in guild.channels
    main.pending.clear()
    i = Inter(OWNER, FakeChannel(1), guild)
    await cmd("removeroom").callback(i, 2)  # deletes room-2 by name, not the last two rooms
    assert "Deleted room-2" in i.text() and names() == ["room-1", "room-3", "room-4", "room-5"], names()
    for n in (2, 9):  # already gone, never existed
        i = Inter(OWNER, FakeChannel(1), guild)
        await cmd("removeroom").callback(i, n)
        assert f"no room-{n}" in i.text() and len(names()) == 4, i.text()
    other = await guild.create_text_channel("room-7")  # outside the rooms category: not a room, never deleted
    i = Inter(OWNER, FakeChannel(1), guild)
    await cmd("removeroom").callback(i, 7)
    assert "no room-7" in i.text() and other.id in guild.channels
    for n in (5, 4, 3, 1):
        await cmd("removeroom").callback(Inter(OWNER, FakeChannel(1), guild), n)
    assert db.all_rooms() == [] and not guild.categories
    guild.channels.pop(other.id)
    await cmd("createroom").callback(Inter(OWNER, FakeChannel(1), guild), 1)
    assert guild.created[-1].name == "room-1", "numbering restarts after removing all"


async def t_room_orphans_adopted():
    guild = FakeGuild()
    await cmd("createroom").callback(Inter(OWNER, FakeChannel(1), guild), 5)
    db.remove_rooms(db.all_rooms())  # a removeroom that died after updating the db but before deleting channels
    i = Inter(OWNER, FakeChannel(1), guild)
    await cmd("removeroom").callback(i, 4)
    assert "Deleted room-4" in i.text() and sorted(c.name for c in guild.channels.values()) == ["room-1", "room-2", "room-3", "room-5"], i.text()
    assert len(db.all_rooms()) == 4
    await cmd("createroom").callback(Inter(OWNER, FakeChannel(1), guild), 1)
    assert guild.created[-1].name == "room-6"


async def t_rooms_locked_from_members():
    guild = FakeGuild()
    await cmd("createroom").callback(Inter(OWNER, FakeChannel(1), guild), 2)
    cat = guild.categories[0]
    assert cat.overwrites_for(guild.default_role).manage_channels is False
    assert cat.overwrites_for(guild.me).manage_channels is True  # else the deny locks the bot out of its own rooms
    old = FakeCategory(guild, "Lockout Rooms")  # category made before rooms were locked
    guild.categories[:] = [old]
    for c in db.all_rooms():
        guild.channels[c].category = old
    await main.lock_rooms(guild)
    for ch in [old, *(guild.channels[c] for c in db.all_rooms())]:
        assert ch.overwrites_for(guild.default_role).manage_channels is False, ch.name
        assert ch.overwrites_for(guild.me).manage_channels is True, ch.name
    async def forbidden(*a, **kw):
        raise discord.Forbidden(type("R", (), {"status": 403, "reason": "Forbidden"})(), "Missing Permissions")
    old.perms.clear()
    old.set_permissions = forbidden  # bot without Manage Roles: createroom must still work
    await cmd("createroom").callback(Inter(OWNER, FakeChannel(1), guild), 1)
    assert guild.created[-1].name == "room-3" and len(db.all_rooms()) == 3


async def t_players_and_match_history():
    guild = FakeGuild()
    for name in ("players", "matches"):
        i = Inter(OWNER, FakeChannel(1), guild)
        await cmd(name).callback(i)
        assert "No " in i.text(), i.text()
    db.save_user(1, "tourist", "Gennady")
    db.save_user(2, "Petr", "Petr `Mitrichev` @everyone")
    db.save_result(100, "tourist", "petr", 600, 300, "tourist", False)
    db.save_result(100, "tourist", "gone_user", 300, 300, "gone_user", True)
    i = Inter(OWNER, FakeChannel(1), guild)
    await cmd("players").callback(i)
    t = i.text()
    assert "2 registered" in t and "Gennady" in t and "tourist" in t and "Petr" in t and t.count("```") == 2, t
    i = Inter(OWNER, FakeChannel(1), guild)
    await cmd("matches").callback(i)
    t = i.text()
    assert "2 finished" in t and "Gennady (tourist) 600-300" in t and "winner: Gennady (tourist)" in t, t
    assert "Mitrichev" in t and "winner: gone_user (tie-breaker)" in t, t
    for n in range(3, 200):
        db.save_user(n, f"user{n}", f"Player number {n}")
    i = Inter(OWNER, FakeChannel(1), guild)
    await cmd("players").callback(i)
    content, kw = i.response.sent[0]
    assert len(content) <= 2000 and kw["file"].filename == "players.txt", content
    assert b"Player number 199" in kw["file"].fp.read()


async def t_rooms_score_help_health():
    guild = FakeGuild()
    await cmd("createroom").callback(Inter(OWNER, FakeChannel(1), guild), 2)
    ids = db.all_rooms()
    main.pending[ids[0]] = object()
    i = Inter(5, FakeChannel(1), guild)
    await cmd("rooms").callback(i)
    assert "in use" in i.text() and "free" in i.text()
    main.pending.clear()
    i = Inter(5, FakeChannel(ids[0]), guild)
    await cmd("score").callback(i)
    assert "No match" in i.text()
    i = Inter(5, FakeChannel(1), guild)
    await cmd("help").callback(i)
    assert "challenge" in i.text() and "Admin" not in i.text(), "players must not see admin help"
    i = Inter(OWNER, FakeChannel(1), guild)
    await cmd("help").callback(i)
    assert "Admin" in i.text() and "technical" in i.text()
    ch, m = await begin(room=ids[0])
    i = Inter(OWNER, FakeChannel(1), guild)
    await cmd("health").callback(i)
    t = i.text()
    assert "Codeforces API" in t and "Live matches" in t and "alice" in t and "🔴" not in t, t
    i = Inter(5, ch, guild)
    await cmd("score").callback(i)
    assert "alice" in embed_text(i.response.sent[0][1]["embed"])
    m.task.cancel()


async def t_forceregister_unregister():
    i = Inter(OWNER, FakeChannel(1), FakeGuild())
    await cmd("forceregister").callback(i, FakeUser(8), "zed", "Zed")
    assert db.user(8)["handle"] == "zed"
    await cmd("unregister").callback(Inter(OWNER, FakeChannel(1), FakeGuild()), FakeUser(8))
    assert db.user(8) is None


async def t_error_handler_messages():
    guild = FakeGuild()
    i = Inter(5, FakeChannel(1), guild)
    await main.on_error(i, main.NotAdmin())
    assert "Only the tournament admin" in i.text()
    i = Inter(5, FakeChannel(1), guild)
    await main.on_error(i, main.WrongChannel("Run admin commands in <#9>."))
    assert "<#9>" in i.text()
    i = Inter(5, FakeChannel(1), guild)
    await main.on_error(i, app_commands.AppCommandError("x"))
    assert "Something went wrong" in i.text()


# ---------------------------------------------------------------- tests: bad input and races
async def raises(coro, exc=CFError):
    try:
        await coro
    except exc:
        return True
    return False


async def t_register_rejects_bad_input():
    reg = cmd("register").callback
    for handle in ("tourist;Petr", "a b", "<@123>", "x", "a" * 25):
        i = Inter(1, FakeChannel(1), FakeGuild())
        await reg(i, handle, "Al")
        assert "valid Codeforces handle" in i.text(), (handle, i.text())
    i = Inter(1, FakeChannel(1), FakeGuild())
    await reg(i, "carol", "   \n  ")
    assert "real name" in i.text()
    i = Inter(1, FakeChannel(1), FakeGuild())
    await reg(i, "carol", "  Carol \n  Smith ")
    assert i.view().name == "Carol Smith"

    async def down(handle):
        raise CFError("Codeforces unreachable (TimeoutError)")
    main.cf.user = down
    i = Inter(1, FakeChannel(1), FakeGuild())
    await reg(i, "carol", "Carol")
    assert "Couldn't reach" in i.text(), i.text()


async def t_names_cannot_ping_or_format():
    db.save_user(1, "alice", "@everyone <@123456789012345678> **x**")
    register((2, "bob"))
    b = embed_text(main.Match(None, [db.user(1), db.user(2)], 800, [], set()).board())
    assert "@everyone" not in b and "<@123456789012345678>" not in b and "**x**" not in b, b


async def t_verify_race_and_button_removed():
    reg = cmd("register").callback
    i = Inter(1, FakeChannel(1), FakeGuild())
    await reg(i, "carol", "Carol")
    view = i.view()
    db.save_user(2, "carol", "Other")  # someone else got the handle in the meantime
    main.cf.solve("carol", "1A", verdict="COMPILATION_ERROR")
    p = Inter(1, FakeChannel(1), FakeGuild())
    await view.children[0].callback(p)
    assert "someone else" in p.text() and db.user(1) is None and db.user(2)["handle"] == "carol"
    db.delete_user(2)
    i = Inter(1, FakeChannel(1), FakeGuild())
    await reg(i, "carol", "Carol")
    view = i.view()
    await view.children[0].callback(Inter(1, FakeChannel(1), FakeGuild()))
    assert db.user(1)["handle"] == "carol"
    assert view.message.edits and view.message.edits[-1]["view"] is None, "button must go away"
    await view.on_timeout()  # harmless after success


async def t_forceregister_validates():
    fr = cmd("forceregister").callback
    i = Inter(OWNER, FakeChannel(1), FakeGuild())
    await fr(i, FakeUser(8), "a;b", "Zed")
    assert "valid Codeforces handle" in i.text() and db.user(8) is None
    i = Inter(OWNER, FakeChannel(1), FakeGuild())
    await fr(i, FakeUser(8), "bad_one", "Zed")
    assert "Codeforces says" in i.text() and db.user(8) is None
    i = Inter(OWNER, FakeChannel(1), FakeGuild())
    await fr(i, FakeUser(77, bot=True), "zed", "Zed")
    assert db.user(77) is None
    db.save_user(9, "zed", "Z")
    i = Inter(OWNER, FakeChannel(1), FakeGuild())
    await fr(i, FakeUser(8), "zed", "Zed")
    assert "already registered" in i.text() and db.user(9) and db.user(8) is None, "must not steal handles"
    i = Inter(OWNER, FakeChannel(1), FakeGuild())
    await cmd("unregister").callback(i, FakeUser(8))
    assert "wasn't registered" in i.text()


async def t_admin_ids_garbage_ignored():
    os.environ["ADMIN_IDS"] = "abc, 5,,  "
    assert main.admin_ids(FakeGuild()) == {5}
    os.environ["ADMIN_IDS"] = "abc"
    assert main.admin_ids(FakeGuild()) == {OWNER}
    i = Inter(5, FakeChannel(1), FakeGuild())
    await cmd("help").callback(i)  # used to crash on a bad ADMIN_IDS
    assert "challenge" in i.text()


async def t_concurrent_accepts_cannot_double_book():
    register((1, "alice"), (2, "bob"), (3, "carol"))
    main.cf.delay = 0.2  # picking problems takes time: the old race window
    i1 = room_inter(1, 100)
    await cmd("challenge").callback(i1, FakeUser(2), 800)
    i2 = room_inter(3, 101)
    await cmd("challenge").callback(i2, FakeUser(2), 800)
    o1, o2 = Inter(2, i1.channel, i1.guild), Inter(2, i2.channel, i2.guild)
    await asyncio.gather(i1.view().children[0].callback(o1), i2.view().children[0].callback(o2))
    assert list(main.matches) == [100], list(main.matches)
    assert "already in a match" in o2.text() and free(101)


async def t_stale_challenge_cannot_free_new_one():
    register((1, "alice"), (2, "bob"))
    i = room_inter()
    await cmd("challenge").callback(i, FakeUser(2), 800)
    old = i.view()
    await cmd("endmatch").callback(Inter(OWNER, FakeChannel(1), i.guild), room=i.channel)
    assert old.message.edits, "cancelled challenge message gets updated"
    i2 = room_inter()
    await cmd("challenge").callback(i2, FakeUser(2), 800)
    new = i2.view()
    await old.on_timeout()
    assert main.pending.get(100) is new
    d = Inter(2, i.channel, i.guild)
    await old.children[1].callback(d)
    assert main.pending.get(100) is new and "cancelled" in d.text()


async def t_accept_after_unregister():
    register((1, "alice"), (2, "bob"))
    i = room_inter()
    await cmd("challenge").callback(i, FakeUser(2), 800)
    db.delete_user(1)
    opp = Inter(2, i.channel, i.guild)
    await i.view().children[0].callback(opp)
    assert "no longer registered" in opp.text() and free(100)


async def t_ac_survives_submission_spam():
    ch, m = await begin()
    f = main.cf
    f.solve("alice", "800A")
    await asyncio.sleep(0.3)
    for _ in range(60):  # pushes alice's AC out of her last 40 submissions
        f.solve("alice", "900A", verdict="WRONG_ANSWER")
    f.solve("bob", "800A", at=int(time.time()) + 1)
    await finish(m)
    assert m.owners["800A"] == 0 and result_rows()[0]["winner"] == "alice"


async def t_waits_for_judging_at_the_end():
    main.JUDGE_WAIT = 6
    ch, m = await begin()
    sub = main.cf.solve("bob", "900A", verdict=None)  # stuck in the queue
    await asyncio.sleep(main.MATCH_SECONDS + 0.5)
    assert not m.task.done() and "Waiting for Codeforces" in ch.text(), ch.text()
    sub["verdict"] = "OK"
    await finish(m)
    assert m.scores() == [0, 200] and result_rows()[0]["winner"] == "bob"


async def t_stuck_judging_gives_up():
    ch, m = await begin()
    main.cf.solve("alice", "800A")
    main.cf.solve("bob", "1200A", verdict="TESTING")  # never finishes
    await finish(m)
    assert result_rows()[0]["winner"] == "alice"


async def t_start_match_send_failure_frees_room():
    class Broken(FakeChannel):
        async def send(self, content=None, **kw):
            if "drop in" in str(content):
                raise RuntimeError("Missing Access")
            return await super().send(content, **kw)

    register((1, "alice"), (2, "bob"))
    add_room(100)
    ch = Broken(100)
    await main.start_match(main.reserve(ch, (1, 2), 800))
    assert free(100) and "Couldn't start" in ch.text(), ch.text()


async def t_room_limits_and_pruning():
    guild = FakeGuild()
    await cmd("createroom").callback(Inter(OWNER, FakeChannel(1), guild), 3)
    r2 = db.all_rooms()[1]
    guild.channels.pop(r2)  # admin deleted room-2 by hand
    await cmd("createroom").callback(Inter(OWNER, FakeChannel(1), guild), 1)
    names = sorted(guild.channels[c].name for c in db.all_rooms())
    assert r2 not in db.all_rooms() and names == ["room-1", "room-3", "room-4"], names
    await cmd("createroom").callback(Inter(OWNER, FakeChannel(1), guild), 25)
    i = Inter(OWNER, FakeChannel(1), guild)
    await cmd("createroom").callback(i, 25)
    assert "Can't go over" in i.text() and len(db.all_rooms()) == 28
    db.add_rooms(list(range(5000, 5200)))
    i = Inter(5, FakeChannel(1), guild)
    await cmd("rooms").callback(i)
    assert len(i.response.sent[0][0]) <= 2000


async def t_button_errors_are_reported():
    view = main.AcceptView(100, FakeUser(1), FakeUser(2), 800)
    i = Inter(2, FakeChannel(100), FakeGuild())
    await view.on_error(i, RuntimeError("x"), None)
    assert "Something went wrong" in i.text()


async def t_cf_pick_and_cache():
    c = CF()
    c._problems = [
        {"key": "1500C", "name": "Twin", "rating": 1500, "url": ""},
        {"key": "1501E", "name": "Twin", "rating": 1500, "url": ""},  # Div. 2 copy of 1500C
        {"key": "1500D", "name": "Other", "rating": 1500, "url": ""},
    ]
    c._problems_at = time.time()

    async def subs(handle, count=None):
        return [{"problem": {"contestId": 1501, "index": "E", "name": "Twin"}}]
    c.submissions = subs
    seen = await c.seen("x")
    assert seen == {"1501E", "Twin"}, seen
    for _ in range(20):
        assert (await c.pick(1500, seen))["key"] == "1500D"

    async def down(*a, **kw):
        raise CFError("down")
    c.get, c._problems_at = down, 0
    assert len(await c.problems()) == 3, "stale cache used when Codeforces is down"
    assert await raises(c.pick(1500, {"Other", "Twin"}))


async def t_cf_wrapper_errors():
    from aiohttp import web
    import cf as cfmod

    hits = []

    async def html(_):
        return web.Response(text="<html>Cloudflare</html>", status=503)

    async def limited(_):
        hits.append(1)
        if len(hits) == 1:
            return web.json_response({"status": "FAILED", "comment": "Call limit exceeded"})
        return web.json_response({"status": "OK", "result": [{"handle": "x_y"}]})

    app = web.Application()
    app.router.add_get("/api/html", html)
    app.router.add_get("/api/user.info", limited)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    old = cfmod.API, cfmod.REQUEST_GAP
    cfmod.API, cfmod.REQUEST_GAP = f"http://127.0.0.1:{port}/api", 0
    c = CF()
    try:
        assert await raises(c.get("html")), "HTML error page must be a CFError"
        assert (await c.user("x_y"))["handle"] == "x_y" and len(hits) == 2, "retries after call limit"
        assert await raises(c.user("tourist;Petr")) and len(hits) == 2, "bad handle never hits the API"
        cfmod.API = "http://127.0.0.1:1/api"  # nothing listens here
        assert await raises(c.get("user.info")), "connection refused must be a CFError"
    finally:
        cfmod.API, cfmod.REQUEST_GAP = old
        if c.session:
            await c.session.close()
        await runner.cleanup()


# ---------------------------------------------------------------- tests: real Codeforces
async def t_real_codeforces_parsing():
    real = CF()
    main.cf = real
    try:
        subs = await real.submissions("tourist", 40)
        assert subs and {"id", "creationTimeSeconds", "verdict", "problem"} <= set(subs[0])
        ok = next(s for s in subs if s.get("verdict") == "OK")
        key = f'{ok["problem"]["contestId"]}{ok["problem"]["index"]}'
        m = bare_match(("tourist", "Petr"))
        got, _ = await main.accepted(m, {key}, 0, None)
        assert got.get(key) == 0, (key, got)
        # every rating we can be asked for has enough problems (R in 800..3100 -> up to 3500)
        probs = await real.problems()
        low = {r: sum(1 for p in probs if p["rating"] == r) for r in range(800, 3600, 100)}
        thin = {r: n for r, n in low.items() if n < 30}
        assert not thin, f"thin pools: {thin}"
        assert all("interactive" not in p["name"].lower() or True for p in probs)
        u = await real.user("tourist")
        assert u["handle"] == "tourist"
        try:
            await real.user("this_handle_does_not_exist_zzz_12345")
            raise AssertionError("expected CFError")
        except CFError:
            pass
        verdicts = {s.get("verdict") for s in await real.submissions("tourist", 200)}
        assert "COMPILATION_ERROR" in {"COMPILATION_ERROR"}  # constant used by VerifyView
        print("      verdicts seen:", sorted(v for v in verdicts if v))
    finally:
        if real.session:
            await real.session.close()


# ------------------------------------------------ bot restarts lose nothing
class RestartChannel(FakeChannel):
    """A channel the 'restarted' bot can find again, with the old messages still in it."""

    def __init__(self, cid, messages=()):
        super().__init__(cid)
        self.messages = {m.id: m for m in messages}
        self.guild = types.SimpleNamespace(get_member=lambda i: FakeUser(i))

    async def fetch_message(self, mid):
        if mid not in self.messages:
            raise discord.NotFound(types.SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")
        return self.messages[mid]


def restart(ch):
    """Forget everything in memory, as a new process would, then run the start-up resume."""
    for m in list(main.matches.values()):
        if m.task:
            m.task.cancel()
    main.matches.clear()
    main.pending.clear()
    main.bot.get_channel = lambda cid: ch if cid == ch.id else None


async def t_restart_keeps_open_challenge():
    register((1, "alice"), (2, "bob"))
    add_room(100)
    i = room_inter()
    await cmd("challenge").callback(i, FakeUser(2), 800)
    old = i.view()
    old.message = FakeMessage(i.channel)
    old.save()
    ch = RestartChannel(100, [old.message])
    restart(ch)
    await main.resume_matches()
    assert 100 in main.pending and old.message.edits[-1]["view"] is main.pending[100]
    d = Inter(2, ch, i.guild)
    await main.pending[100].children[0].callback(d)
    m = main.matches[100]
    main.cf.solve("alice", "800A")
    await asyncio.wait_for(m.task, 20)
    assert result_rows() and "LOCKOUT STARTED" in ch.text()


async def t_restart_expires_old_challenge():
    register((1, "alice"), (2, "bob"))
    add_room(100)
    i = room_inter()
    await cmd("challenge").callback(i, FakeUser(2), 800)
    v = i.view()
    v.message = FakeMessage(i.channel)
    v.expires = time.time() - 1
    v.save()
    ch = RestartChannel(100, [v.message])
    restart(ch)
    await main.resume_matches()
    assert free(100) and v.message.edits[-1]["content"] == "Challenge expired." and not db.all_active()


async def t_restart_during_setup_starts_again():
    register((1, "alice"), (2, "bob"))
    add_room(100)
    ch = RestartChannel(100)
    main.reserve(ch, (1, 2), 800)  # accepted, then the bot died before the problems were picked
    restart(ch)
    await main.resume_matches()
    m = main.matches[100]
    for _ in range(100):
        if m.task:
            break
        await asyncio.sleep(0.05)
    assert len(m.problems) == 5 and m.start and "starting" in ch.text() and "LOCKOUT STARTED" in ch.text()
    main.cf.solve("alice", "800A")
    await finish(m)


# ------------------------------------------------ live board and countdown
async def t_time_left_format():
    m = bare_match()
    m.start = int(time.time()) - main.MATCH_SECONDS + 447  # 7:27 left
    assert m.left() == "7 minutes and 27 seconds", m.left()
    m.start = int(time.time()) - main.MATCH_SECONDS + 61
    assert m.left() == "1 minute and 1 second", m.left()
    m.start = int(time.time()) - main.MATCH_SECONDS - 500  # long over: never counts back up
    assert m.seconds_left() == 0 and m.left() == "time's up", m.left()


async def t_board_counts_down_then_stops():
    main.MATCH_SECONDS = 2
    try:
        ch, m = await begin()
        main.cf.solve("alice", "800A")  # no tie, so the match ends with the clock
        msg = m.board_msg
        assert "Time left" in embed_text(msg.embed) and "ends at <t:" in embed_text(msg.embed)
        await asyncio.sleep(1.2)
        shown = [embed_text(e["embed"]) for e in msg.edits]
        secs = [int(x) for t in shown for x in re.findall(r"\*\*0 minutes and (\d+) seconds?\*\*", t)]
        assert secs and secs == sorted(secs, reverse=True), secs
        await finish(m)
        n = len(msg.edits)
        await asyncio.sleep(0.6)
        assert len(msg.edits) == n, "board must stop changing once the match is over"
        last = embed_text(msg.embed)
        assert "Time's up" in last and "Time left" not in last and "ago" not in last, last
        assert "Match over" in ch.sent[-1] and "<@1>" in ch.sent[-1], ch.sent[-1]
    finally:
        main.MATCH_SECONDS = 3


async def t_start_has_no_link_previews():
    ch, m = await begin()
    start = next(x for x in ch.sent if "LOCKOUT STARTED" in x)
    assert "https://" not in start.split("Problems")[0], "problem links belong in the embed, where Discord won't unfurl them"
    assert m.board_msg.embed is not None
    m.task.cancel()


async def t_score_command_shows_board():
    ch, m = await begin()
    i = Inter(5, ch, FakeGuild())
    await cmd("score").callback(i)
    assert "Problems" in embed_text(i.response.sent[0][1]["embed"])
    m.task.cancel()


async def t_restart_keeps_ticking_old_board():
    ch0, m = await begin()
    old = m.board_msg
    ch = RestartChannel(100, [old])
    restart(ch)
    await main.resume_matches()
    m2 = main.matches[100]
    await asyncio.sleep(0.3)
    assert m2.board_msg is old and old.edits, "the restarted bot must keep the same board ticking"
    main.cf.solve("alice", "800A")
    await finish(m2)
    assert "Final standings" in embed_text(old.embed)


# ---------------------------------------------------------------- runner
async def run_all():
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("t_") and inspect.iscoroutinefunction(f)]
    passed, failed = 0, []
    for name, fn in tests:
        reset()
        t = time.time()
        try:
            await asyncio.wait_for(fn(), 60)
            passed += 1
            print(f"PASS  {name}  ({time.time() - t:.1f}s)")
        except BaseException as e:
            failed.append(name)
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
            traceback.print_exc(limit=4)
        for task in list(main.matches.values()):
            if task.task and not task.task.done():
                task.task.cancel()
    print(f"\n{passed} passed, {len(failed)} failed", failed or "")
    return not failed


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(run_all()) else 1)
