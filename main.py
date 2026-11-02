import asyncio
import io
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field

import discord
from discord import app_commands
from dotenv import load_dotenv

import db
from cf import CF, CFError, valid_handle

load_dotenv()
log = logging.getLogger("lockout")
logging.basicConfig(level=logging.INFO)

MATCH_SECONDS = int(float(os.getenv("MATCH_MINUTES") or "15") * 60)  # default length when /challenge gives no minutes
GRACE = 20  # let Codeforces finish judging last-second submissions
JUDGE_WAIT = 180  # after GRACE, keep waiting this long for submissions still in the judging queue
POLL = 8
TICK = 3  # seconds between edits of the board's "time left" (Discord rate-limits faster edits)
COUNTDOWN = 10
FINAL_RETRIES = 6  # failed Codeforces checks after time is up before we hand over to organizers
CHALLENGE_SECONDS = 120
VERIFY_PROBLEM = (1, "A")  # player submits a compile error here to prove handle ownership
VERIFY_SECONDS = 300
MAX_ROOMS = 50  # Discord allows 50 channels per category


@dataclass(frozen=True)
class Kind:
    category: str
    prefix: str
    table: str
    label: str


ROOMS = Kind("Lockout Rooms", "room", "rooms", "room")
EXTRA = Kind("Extra Rooms", "extra", "extra_rooms", "extra room")  # practice: same game, results never recorded
MAX_MESSAGE = 2000
ERROR_MSG = "Something went wrong. Try again, or tell an organizer."

cf = CF()
matches: dict[int, "Match"] = {}  # channel_id -> match (start == 0 while problems are being picked)
pending: dict[int, "AcceptView"] = {}  # channel_id -> open challenge


def busy(channel_id: int) -> bool:
    return channel_id in matches or channel_id in pending


def playing() -> set[int]:
    """Discord IDs of everyone in a starting or running match."""
    return {p["discord_id"] for m in matches.values() for p in m.players}


def safe(text) -> str:
    """User-supplied text (names, handles) shown in chat: no pings, no markdown tricks."""
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(text)))


def clip(text: str) -> str:
    return text if len(text) <= MAX_MESSAGE else text[: MAX_MESSAGE - 20] + "\n… (truncated)"


def used(problems: list) -> set[str]:
    return {v for p in problems for v in (p["key"], p["name"])}


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


LIVE, DONE, NEWS, WIN, TIE = 0x57F287, 0x99AAB5, 0x5865F2, 0xF1C40F, 0xE67E22  # embed side colours


def mentions(m: "Match") -> str:
    return " ".join(f"<@{p['discord_id']}>" for p in m.players)


@dataclass
class Match:
    channel: discord.abc.Messageable
    players: list  # two db user rows
    rating: int
    problems: list
    seen: set
    start: int = 0
    owners: dict = field(default_factory=dict)  # problem key -> player index
    task: asyncio.Task | None = None
    found: set = field(default_factory=set)  # every (time, id, key, player) AC seen so far
    tie: dict | None = None  # tie-breaker problem once picked
    tie_since: int = 0
    board_id: int = 0  # the live board message, edited as the clock runs and problems get solved
    board_msg: discord.Message | None = None
    seconds: int = field(default_factory=lambda: MATCH_SECONDS)  # match length, chosen in /challenge
    practice: bool = False  # played in an extra room: never recorded, nobody advances

    def name(self, i: int) -> str:
        p = self.players[i]
        return f'**{safe(p["real_name"])}** ({safe(p["handle"])})'

    def scores(self) -> list[int]:
        sc = [0, 0]
        for i, p in enumerate(self.problems):
            if (o := self.owners.get(p["key"])) is not None:
                sc[o] += 100 * (i + 1)
        return sc

    def seconds_left(self) -> int:
        return max(0, math.ceil(self.start + self.seconds - time.time()))

    def left(self) -> str:
        rest = self.seconds_left()
        return f"{plural(rest // 60, 'minute')} and {plural(rest % 60, 'second')}" if rest else "time's up"

    def scoreline(self) -> str:
        a, b = self.scores()
        return f"{self.name(0)} (**{a}** points) vs (**{b}** points) {self.name(1)}"

    def board(self, note: str = "") -> discord.Embed:
        """The Problems table. While the clock runs it shows the time left; afterwards `note` says what's happening."""
        points, names, ratings = [], [], []
        for i, p in enumerate(self.problems):
            o = self.owners.get(p["key"])
            points.append(str(100 * (i + 1)))
            names.append(f"✅ Solved by {safe(self.players[o]['handle'])}" if o is not None else f'[{p["name"]}]({p["url"]})')
            ratings.append(str(p["rating"]))
        e = discord.Embed(title="Problems", description=self.scoreline(), color=LIVE if self.seconds_left() else DONE)
        e.add_field(name="Points", value="\n".join(points) or "-")
        e.add_field(name="Problem Name", value="\n".join(names) or "-")
        e.add_field(name="Rating", value="\n".join(ratings) or "-")
        if self.seconds_left():
            e.add_field(name="⏱️ Time left", value=f"**{self.left()}** · ends at <t:{self.start + self.seconds}:T>", inline=False)
        else:
            e.add_field(name="⏱️ Time's up", value=note or "Checking the final verdicts on Codeforces…", inline=False)
        return e

    async def refresh(self, note: str = ""):
        """Re-draw the live board in place."""
        if not self.board_msg:
            return
        try:
            await self.board_msg.edit(embed=self.board(note))
        except discord.NotFound:
            self.board_msg = None  # someone deleted it; stop editing a message that's gone
        except discord.HTTPException as e:
            log.warning("board edit failed: %s", e)

    def save(self):
        """Written to the database so a bot restart can pick the match back up."""
        db.save_active(self.channel.id, json.dumps({
            "players": [p["discord_id"] for p in self.players], "rating": self.rating, "problems": self.problems,
            "seen": sorted(self.seen), "start": self.start, "owners": self.owners, "found": sorted(self.found),
            "tie": self.tie, "tie_since": self.tie_since, "board": self.board_id, "seconds": self.seconds,
            "practice": self.practice,
        }))


def restore(channel, data: str) -> Match | None:
    d = json.loads(data)
    players = [db.user(i) for i in d["players"]]
    if not all(players):
        return None
    seconds, practice = d.get("seconds", MATCH_SECONDS), d.get("practice", False)
    if not d["start"]:  # still picking problems or counting down: start over
        return Match(channel, players, d["rating"], [], set(), seconds=seconds, practice=practice)
    return Match(channel, players, d["rating"], d["problems"], set(d["seen"]), d["start"], d["owners"],
                 found={tuple(f) for f in d["found"]}, tie=d.get("tie"), tie_since=d.get("tie_since", 0),
                 board_id=d.get("board", 0), seconds=seconds, practice=practice)


async def accepted(m: Match, keys: set[str], since: int, until: int | None) -> tuple[dict[str, int], bool]:
    """First accepted submitter per problem key within [since, until], and whether any
    submission in that window is still being judged.

    ACs are remembered on the match, so one that scrolls out of the recent-submissions
    window (a player spamming submissions) still counts.
    """
    judging = False
    for i, p in enumerate(m.players):
        for s in await cf.submissions(p["handle"], 40):
            pr = s["problem"]
            k = f'{pr.get("contestId")}{pr["index"]}'
            t = s["creationTimeSeconds"]
            if k not in keys or t < since or (until is not None and t > until):
                continue
            if s.get("verdict") == "OK":
                m.found.add((t, s["id"], k, i))
            elif s.get("verdict") in (None, "TESTING"):
                judging = True
    first: dict[str, int] = {}
    for t, _, k, i in sorted(m.found):
        if k in keys and t >= since and (until is None or t <= until):
            first.setdefault(k, i)
    return first, judging


async def attach_board(m: Match):
    """After a restart: keep editing the old board, or post a fresh one if it's gone."""
    if m.board_msg:
        return
    try:
        if m.board_id:
            try:
                m.board_msg = await m.channel.fetch_message(m.board_id)
                return
            except discord.HTTPException:
                pass
        m.board_msg = await m.channel.send(embed=m.board())
        m.board_id = m.board_msg.id
        m.save()
    except discord.HTTPException as e:
        log.warning("could not post board: %s", e)


async def countdown(m: Match):
    """Ticks the board's "time left" down until the clock runs out, then shows "Time's up" (never counts back up)."""
    while True:
        await m.refresh()
        rest = m.start + m.seconds - time.time()
        if rest <= 0:
            return
        await asyncio.sleep(min(TICK, rest))


def result(m: Match, winner: int, tied: bool) -> discord.Embed:
    a, b = m.scores()
    how = "wins the tie-breaker" if tied else "wins"
    if m.practice:
        return discord.Embed(
            title="🏁 Practice match over", color=WIN,
            description=f"🏆 {m.name(winner)} {how}!\n\n**Final score:** {m.name(0)} **{a}** – **{b}** {m.name(1)}\n\n"
                        "_Practice match: it doesn't count for the competition._",
        )
    return discord.Embed(
        title="🏁 Match over", color=WIN,
        description=f"🏆 {m.name(winner)} {how} and **advances**!\n❌ {m.name(1 - winner)} is knocked out.\n\n"
                    f"**Final score:** {m.name(0)} **{a}** – **{b}** {m.name(1)}",
    )


async def run_match(m: Match):
    channel_id = m.channel.id
    shutting_down = False
    ticker = None
    try:
        await attach_board(m)
        ticker = asyncio.create_task(countdown(m))
        end = m.start + m.seconds
        keys = {p["key"] for p in m.problems}
        final_fails = 0
        told_judging = False
        while True:
            now = time.time()
            final = now >= end + GRACE
            try:
                first, judging = await accepted(m, keys, m.start, end)
            except Exception as e:
                log.warning("poll failed: %s", e)
                if final:
                    final_fails += 1
                    if final_fails >= FINAL_RETRIES:
                        ticker.cancel()
                        await m.refresh("Couldn't reach Codeforces to confirm the result.")
                        await m.channel.send(
                            f"{mentions(m)} ⚠️ Can't reach Codeforces to confirm the final result. "
                            "Organizers, please check the scores and decide manually.",
                            embed=m.board("Couldn't reach Codeforces to confirm the result."),
                        )
                        return
                await asyncio.sleep(POLL)
                continue
            for k, i in first.items():
                if m.owners.get(k) != i:
                    m.owners[k] = i
                    m.save()
                    idx = next(n for n, p in enumerate(m.problems) if p["key"] == k)
                    await m.refresh()
                    await m.channel.send(f"{mentions(m)}, there is an update in standings!", embed=discord.Embed(
                        description=f"<@{m.players[i]['discord_id']}> has solved problem {idx + 1} worth "
                                    f"**{100 * (idx + 1)}** points\n\n{m.scoreline()}",
                        color=NEWS,
                    ))
            if final:
                if not judging or now >= end + GRACE + JUDGE_WAIT:
                    break
                if not told_judging:
                    told_judging = True
                    await m.channel.send("⏳ Time's up. Waiting for Codeforces to finish judging the last submissions…")
            await asyncio.sleep(POLL)

        ticker.cancel()
        a, b = m.scores()
        tied = a == b
        await m.refresh("Scores are level: tie-breaker in progress." if tied else "Final standings.")
        winner = (await tiebreak(m)) if tied else (0 if a > b else 1)
        if winner is None:
            await m.channel.send("⚠️ Couldn't get a tie-breaker problem from Codeforces. Organizers, please decide manually.")
            return
        if not m.practice:  # practice matches never touch the competition's results
            db.save_result(channel_id, m.players[0]["handle"], m.players[1]["handle"], a, b,
                           m.players[winner]["handle"], tied)
        db.drop_active(channel_id)  # a restart after this must not replay a finished match
        if tied:
            await m.refresh(f"Final standings. Tie-breaker won by {safe(m.players[winner]['handle'])}.")
        await m.channel.send(f"{mentions(m)}, the match is over!", embed=result(m, winner, tied))
    except asyncio.CancelledError:
        shutting_down = True  # also /lockout endmatch, which drops the saved match itself
        raise
    except Exception:
        log.exception("match crashed")
        await m.channel.send("⚠️ Match crashed. Organizers, please resolve this one manually.")
    finally:
        if ticker:
            ticker.cancel()
        if matches.get(channel_id) is m:
            matches.pop(channel_id)
        if not shutting_down:  # a restart must leave the saved match for on_ready to resume
            db.drop_active(channel_id)


async def tiebreak(m: Match) -> int | None:
    """Index of the first player to solve a fresh problem, or None if none could be picked."""
    if m.tie:  # picked before a restart: same problem, same clock
        p, since = m.tie, m.tie_since
    else:
        for _ in range(FINAL_RETRIES):
            try:
                p = await cf.pick(m.rating + 200, m.seen | used(m.problems))
                break
            except Exception as e:
                log.warning("tiebreak pick failed: %s", e)
                await asyncio.sleep(POLL)
        else:
            return None
        since = int(time.time())
        m.tie, m.tie_since = p, since
        m.save()
    a, _ = m.scores()
    await m.channel.send(f"{mentions(m)}, time's up and it's a tie!", embed=discord.Embed(
        title="⚔️ Tie-breaker", color=TIE,
        description=f"Both players have **{a}** points. The first accepted submission on this problem wins:\n\n"
                    f"[{p['name']}]({p['url']}) ({p['rating']})",
    ))
    while True:
        await asyncio.sleep(POLL)
        try:
            first, _ = await accepted(m, {p["key"]}, since, None)
        except Exception as e:
            log.warning("tiebreak poll failed: %s", e)
            continue
        if p["key"] in first:
            return first[p["key"]]


async def reply(inter: discord.Interaction, msg: str):
    """Ephemeral answer whether or not the interaction was already responded to."""
    try:
        if inter.response.is_done():
            await inter.followup.send(msg, ephemeral=True)
        else:
            await inter.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        log.warning("could not deliver error message")


class View(discord.ui.View):
    async def on_error(self, inter: discord.Interaction, error: Exception, item):
        log.error("button error", exc_info=error)
        await reply(inter, ERROR_MSG)


class AcceptView(View):
    def __init__(self, channel_id, challenger, opponent, rating, seconds=None, left=CHALLENGE_SECONDS):
        super().__init__(timeout=left)
        self.channel_id, self.challenger, self.opponent, self.rating = channel_id, challenger, opponent, rating
        self.seconds = seconds or MATCH_SECONDS
        self.expires = time.time() + left
        self.message = None

    def save(self):
        """Written to the database so a bot restart can bring the Accept button back."""
        db.save_active(self.channel_id, json.dumps({
            "kind": "challenge", "challenger": self.challenger.id, "opponent": self.opponent.id,
            "rating": self.rating, "seconds": self.seconds, "message": self.message.id, "expires": self.expires,
        }))

    def close(self):
        self.stop()
        if pending.get(self.channel_id) is self:
            pending.pop(self.channel_id)
            db.drop_active(self.channel_id)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, inter: discord.Interaction, _):
        if inter.user.id != self.opponent.id:
            return await inter.response.send_message("This challenge isn't for you.", ephemeral=True)
        if pending.get(self.channel_id) is not self:
            self.stop()
            return await inter.response.edit_message(content="This challenge was cancelled.", view=None)
        ids = (self.challenger.id, self.opponent.id)
        now_playing = playing()
        problem = next((f"<@{i}> is already in a match." for i in ids if i in now_playing), None) or next(
            (f"<@{i}> is no longer registered." for i in ids if not db.user(i)), None
        )
        if problem:
            self.close()
            return await inter.response.edit_message(content=f"Challenge cancelled: {problem}", view=None)
        self.stop()
        m = reserve(inter.channel, ids, self.rating, self.seconds)  # synchronous: nobody can grab room or players after this
        await inter.response.edit_message(content="Accepted! Picking problems…", view=None)
        await start_match(m)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, inter: discord.Interaction, _):
        if inter.user.id not in (self.opponent.id, self.challenger.id):
            return await inter.response.send_message("Not your match.", ephemeral=True)
        if pending.get(self.channel_id) is not self:
            self.stop()
            return await inter.response.edit_message(content="This challenge was cancelled.", view=None)
        self.close()
        word = "declined" if inter.user.id == self.opponent.id else "withdrawn"
        await inter.response.edit_message(content=f"Challenge {word}.", view=None)

    async def on_timeout(self):
        if pending.get(self.channel_id) is self:
            pending.pop(self.channel_id)
            db.drop_active(self.channel_id)
            if self.message:
                await self.message.edit(content="Challenge expired.", view=None)


def reserve(channel, ids, rating, seconds=None) -> Match:
    """Claim the room and both players. No awaits, so no other challenge can sneak in."""
    pending.pop(channel.id, None)
    m = Match(channel, [db.user(i) for i in ids], rating, [], set(), seconds=seconds or MATCH_SECONDS,
              practice=db.is_room(channel.id, EXTRA.table))
    matches[channel.id] = m
    m.save()  # replaces the saved challenge, so a restart restarts the setup instead of losing the match
    return m


async def start_match(m: Match):
    cid = m.channel.id
    try:
        for p in m.players:
            m.seen |= await cf.seen(p["handle"])
        for r in range(m.rating, m.rating + 500, 100):
            m.problems.append(await cf.pick(r, m.seen | used(m.problems)))
        if matches.get(cid) is not m:  # organizer cancelled while we were picking
            return
        await m.channel.send(f"{mentions(m)} problems drop in {COUNTDOWN} seconds. Get ready!")
        await asyncio.sleep(COUNTDOWN)
        if matches.get(cid) is not m:  # organizer cancelled during the countdown
            return
        m.start = int(time.time())
        m.board_msg = await m.channel.send(
            f"🚀 **{'PRACTICE ' if m.practice else ''}LOCKOUT STARTED** · {m.seconds / 60:g} minutes · first accepted submission on Codeforces takes the problem.",
            embed=m.board(),
        )
        m.board_id = m.board_msg.id
        m.save()
        m.task = asyncio.create_task(run_match(m))
    except Exception as e:
        log.exception("match start failed")
        if matches.get(cid) is m:
            matches.pop(cid)
            db.drop_active(cid)
        reason = e if isinstance(e, CFError) else "unexpected error"
        await m.channel.send(f"❌ Couldn't start the match: {reason}. The room is free again.")


class VerifyView(View):
    def __init__(self, user_id, handle, name):
        super().__init__(timeout=VERIFY_SECONDS)
        self.user_id, self.handle, self.name = user_id, handle, name
        self.started = int(time.time())
        self.message = None

    @discord.ui.button(label="I've submitted", style=discord.ButtonStyle.primary)
    async def check(self, inter: discord.Interaction, _):
        await inter.response.defer(ephemeral=True)
        try:
            subs = await cf.submissions(self.handle, 10)
        except CFError as e:
            return await inter.followup.send(f"Codeforces error: {e}", ephemeral=True)
        cid, idx = VERIFY_PROBLEM
        ok = any(
            s["problem"].get("contestId") == cid and s["problem"]["index"] == idx
            and s.get("verdict") == "COMPILATION_ERROR" and s["creationTimeSeconds"] >= self.started - 5
            for s in subs
        )
        if not ok:
            return await inter.followup.send("Haven't seen it yet. Wait a few seconds and press again.", ephemeral=True)
        taken = db.by_handle(self.handle)
        if taken and taken["discord_id"] != self.user_id:
            self.stop()
            return await inter.followup.send("That handle was registered by someone else in the meantime.", ephemeral=True)
        db.save_user(self.user_id, self.handle, self.name)
        self.stop()
        await self._close("✅ Verified.")
        await inter.followup.send(f"✅ Verified: **{safe(self.name)}** = `{self.handle}`", ephemeral=True)

    async def on_timeout(self):
        await self._close("⌛ Verification expired. Run `/lockout register` again.")

    async def _close(self, text):
        if self.message:
            try:
                await self.message.edit(content=text, view=None)
            except discord.HTTPException:
                pass


async def resume_challenge(channel, d: dict):
    """Bring a still-open challenge's Accept button back, or close it if it ran out while we were down."""
    cid = channel.id
    try:
        msg = await channel.fetch_message(d["message"])
    except discord.HTTPException:
        return db.drop_active(cid)
    left = d["expires"] - time.time()
    if left <= 0:
        db.drop_active(cid)
        return await msg.edit(content="Challenge expired.", view=None)
    try:
        who = [channel.guild.get_member(i) or await channel.guild.fetch_member(i) for i in (d["challenger"], d["opponent"])]
    except discord.HTTPException:
        db.drop_active(cid)
        return await msg.edit(content="Challenge expired.", view=None)
    view = AcceptView(cid, who[0], who[1], d["rating"], d.get("seconds", MATCH_SECONDS), left)
    view.message = msg
    view.expires = d["expires"]
    pending[cid] = view
    await msg.edit(view=view)
    log.info("resumed challenge in %s", channel)


async def resume_matches():
    """Challenges and matches that were open when the bot stopped carry on where they left off."""
    for cid, data in db.all_active():
        if busy(cid):  # on_ready also fires after a reconnect
            continue
        channel = bot.get_channel(cid)
        try:
            d = json.loads(data)
            if not channel:
                raise ValueError("channel gone")
            if d.get("kind") == "challenge":
                await resume_challenge(channel, d)
                continue
            m = restore(channel, data)
        except (KeyError, ValueError):
            m = None
        except discord.HTTPException as e:
            log.warning("could not resume challenge in %s: %s", cid, e)
            continue
        if not m:
            db.drop_active(cid)
            continue
        matches[cid] = m
        log.info("resumed match in %s", channel)
        try:
            if m.start:
                m.task = asyncio.create_task(run_match(m))
                await channel.send("🔄 Bot restarted. The match carries on, nothing was lost.")
            else:
                asyncio.create_task(start_match(m))
                await channel.send("🔄 Bot restarted while the match was starting. Starting it again.")
        except discord.HTTPException:
            pass


class Bot(discord.Client):
    def __init__(self):
        super().__init__(
            intents=discord.Intents.default(),
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False),
        )
        self.tree = app_commands.CommandTree(self)

    async def on_ready(self):
        await resume_matches()
        for g in self.guilds:
            sync_rooms(g)
            sync_rooms(g, EXTRA)
            try:
                await lock_rooms(g)
                if discord.utils.get(g.categories, name=EXTRA.category):
                    await lock_rooms(g, EXTRA)
            except discord.HTTPException as e:
                log.warning("could not lock rooms in %s: %s", g.name, e)
        log.info("✅ Bot online as %s in %d server(s)", self.user, len(self.guilds))

    async def setup_hook(self):
        if gid := os.getenv("GUILD_ID"):  # guild sync is instant; global can lag
            guild = discord.Object(int(gid))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


bot = Bot()
bot.started = time.time()
lockout = app_commands.Group(name="lockout", description="Lockout tournament", guild_only=True)


def admin_ids(guild: discord.Guild) -> set[int]:
    """ADMIN_IDS from .env (comma separated Discord user IDs); defaults to the server owner."""
    ids = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
    return ids or {guild.owner_id}


def is_admin_user(inter: discord.Interaction) -> bool:
    return inter.user.id in admin_ids(inter.guild)


class NotAdmin(app_commands.CheckFailure):
    pass


class WrongChannel(app_commands.CheckFailure):
    pass


def technical_channel(guild: discord.Guild):
    """The private admin channel, if it was created and still exists."""
    cid = db.get_config("technical")
    return guild.get_channel(int(cid)) if cid else None


def _admin_anywhere(inter: discord.Interaction) -> bool:
    if not is_admin_user(inter):
        raise NotAdmin()
    return True


def _admin_in_technical(inter: discord.Interaction) -> bool:
    _admin_anywhere(inter)
    tech = technical_channel(inter.guild)
    if tech and inter.channel.id != tech.id:
        raise WrongChannel(f"Run admin commands in {tech.mention}.")
    return True


admin_anywhere = app_commands.check(_admin_anywhere)
is_admin = app_commands.check(_admin_in_technical)
bot.tree.add_command(lockout)


@bot.tree.error
async def on_error(inter: discord.Interaction, error: app_commands.AppCommandError):
    orig = getattr(error, "original", None)
    if isinstance(orig, discord.NotFound) and orig.code == 10062:  # Unknown interaction: we missed the 3 second window
        age = (discord.utils.utcnow() - inter.created_at).total_seconds()
        return log.warning("/%s answered %.1fs after it was sent; Discord had already given up", inter.command.qualified_name, age)
    if isinstance(error, WrongChannel):
        msg = str(error)
    elif isinstance(error, app_commands.CheckFailure):
        msg = "Only the tournament admin can use this command."
    elif isinstance(orig, discord.Forbidden):
        log.error("command error", exc_info=error)
        msg = "Discord refused: the bot is missing a permission here. Give its role Manage Channels and Manage Roles, then try again."
    else:
        log.error("command error", exc_info=error)
        msg = ERROR_MSG
    await reply(inter, msg)


def clean_name(name: str) -> str:
    return " ".join(name.split())  # no newlines or runs of spaces


@lockout.command(description="How to use the bot")
async def help(inter: discord.Interaction):
    text = (
        "**🏁 Lockout: how to play**\n"
        "1. `/lockout register handle name`: link your Codeforces handle (one time). "
        "Submit a compile error on the problem it links to prove the handle is yours.\n"
        "2. Go to a free room (`/lockout rooms` shows which).\n"
        f"3. `/lockout challenge @opponent rating [minutes]`: the opponent presses **Accept**. Minutes is optional (default {MATCH_SECONDS / 60:g}).\n"
        f"4. 5 problems appear (rating to rating+400, worth 100 to 500 points).\n"
        "5. First accepted submission on Codeforces takes the problem. Wrong submissions have no penalty.\n"
        "6. Most points wins. A tie gets a tie-breaker problem, and the first to solve it wins.\n"
        "`/lockout score`: live score in the current room.\n"
        "**Practice:** challenge someone in an **extra-N** room (Extra Rooms). Same game, but it doesn't count for the competition."
    )
    if is_admin_user(inter):
        text += (
            "\n\n**🔧 Admin**\n"
            f"`/lockout createroom number`: add rooms in order (max {MAX_ROOMS})\n"
            "`/lockout removeroom number`: delete one room by its number (5 deletes room-5)\n"
            "`/lockout createextraroom number` · `/lockout removeextraroom number`: practice rooms (don't count)\n"
            "`/lockout endmatch [room]`: cancel a match or open challenge\n"
            "`/lockout technical`: create the private admin channel (run admin commands there)\n"
            "`/lockout forceregister @player handle name` · `/lockout unregister @player`\n"
            "`/lockout players`: every registered player and their Codeforces handle\n"
            "`/lockout matches`: every finished match and its winner\n"
            "`/lockout health`: bot status and live matches"
        )
    await inter.response.send_message(text, ephemeral=True)


@lockout.command(description="Link your Codeforces handle and real name")
async def register(inter: discord.Interaction, handle: app_commands.Range[str, 3, 24], name: app_commands.Range[str, 1, 40]):
    name = clean_name(name)
    if not valid_handle(handle):
        return await inter.response.send_message(
            "That isn't a valid Codeforces handle (3–24 letters, digits, `_`, `-` or `.`).", ephemeral=True
        )
    if not name:
        return await inter.response.send_message("Please give your real name.", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    try:
        canon = (await cf.user(handle))["handle"]
    except CFError as e:
        if "not found" in str(e):
            return await inter.followup.send(f"Codeforces handle `{handle}` not found.", ephemeral=True)
        return await inter.followup.send(f"Couldn't reach Codeforces ({e}). Try again in a minute.", ephemeral=True)
    taken = db.by_handle(canon)
    if taken and taken["discord_id"] != inter.user.id:
        return await inter.followup.send("That handle is already registered by someone else.", ephemeral=True)
    cid, idx = VERIFY_PROBLEM
    view = VerifyView(inter.user.id, canon, name)
    view.message = await inter.followup.send(
        f"To prove `{canon}` is yours, submit **any code that fails to compile** (Compilation error) to "
        f"https://codeforces.com/problemset/problem/{cid}/{idx} within {VERIFY_SECONDS // 60} minutes, "
        "then press the button.",
        view=view, ephemeral=True, wait=True,
    )


@lockout.command(description="Challenge someone to a lockout in this room")
@app_commands.describe(minutes=f"Match length in minutes (default {MATCH_SECONDS / 60:g})")
async def challenge(inter: discord.Interaction, opponent: discord.Member, rating: app_commands.Range[int, 800, 3100],
                    minutes: app_commands.Range[int, 1] | None = None):
    cid = inter.channel.id
    practice = db.is_room(cid, EXTRA.table)
    if not (practice or db.is_room(cid)):
        return await inter.response.send_message("Use this in one of the lockout or extra room channels.", ephemeral=True)
    if rating % 100:
        return await inter.response.send_message("Rating must be a multiple of 100.", ephemeral=True)
    if opponent.id == inter.user.id or opponent.bot:
        return await inter.response.send_message("Pick a real opponent.", ephemeral=True)
    for who in (inter.user, opponent):
        if not db.user(who.id):
            return await inter.response.send_message(f"{who.mention} hasn't registered yet (`/lockout register`).", ephemeral=True)
    now_playing = playing()
    for who in (inter.user, opponent):
        if who.id in now_playing:
            return await inter.response.send_message(f"{who.mention} is already in a match.", ephemeral=True)
    if busy(cid):
        return await inter.response.send_message("This room is in use. Try another room.", ephemeral=True)
    seconds = minutes * 60 if minutes else MATCH_SECONDS
    view = AcceptView(cid, inter.user, opponent, rating, seconds)
    pending[cid] = view
    try:
        await inter.response.send_message(
            f"{opponent.mention}, {inter.user.mention} challenges you! Problems **{rating}–{rating + 400}**, {seconds / 60:g} min."
            + (" Practice match: it doesn't count for the competition." if practice else ""),
            view=view,
        )
    except Exception:
        view.close()
        raise
    view.message = await inter.original_response()
    view.save()


@lockout.command(description="Show which rooms are free")
async def rooms(inter: discord.Interaction):
    def listing(kind):
        return "\n".join(f"<#{c}> · {'🔴 in use' if busy(c) else '🟢 free'}" for c in sync_rooms(inter.guild, kind))
    text = listing(ROOMS) or "No rooms yet. An admin can run /lockout createroom."
    if extra := listing(EXTRA):
        text += "\n\n**Extra rooms (practice, doesn't count):**\n" + extra
    await inter.response.send_message(clip(text), ephemeral=True)


@lockout.command(description="Show the live score in this room")
async def score(inter: discord.Interaction):
    m = matches.get(inter.channel.id)
    if not (m and m.start):
        return await inter.response.send_message("No match running here.")
    await inter.response.send_message(embed=m.board())


def room_number(channel, kind: Kind = ROOMS) -> int:
    found = re.fullmatch(rf"{kind.prefix}-(\d+)", getattr(channel, "name", "") or "")
    return int(found.group(1)) if found else 0


def sync_rooms(guild: discord.Guild, kind: Kind = ROOMS) -> list[int]:
    """Room IDs in number order. Drops rooms deleted by hand and adopts room-N channels a failed command left behind."""
    t = kind.table
    db.remove_rooms([c for c in db.all_rooms(t) if not guild.get_channel(c) and not busy(c)], t)
    if cat := discord.utils.get(guild.categories, name=kind.category):
        known = set(db.all_rooms(t))
        db.add_rooms([ch.id for ch in cat.text_channels if room_number(ch, kind) and ch.id not in known], t)
    return sorted(db.all_rooms(t), key=lambda c: room_number(guild.get_channel(c), kind))


LOCKED = discord.PermissionOverwrite(manage_channels=False)  # members can't rename, move or delete rooms
KEEP = discord.PermissionOverwrite(manage_channels=True)  # without it the @everyone deny strips the bot's own Manage Channels too


async def lock_rooms(guild: discord.Guild, kind: Kind = ROOMS):
    """The rooms category, created if missing, with it and every room locked against edits by regular members."""
    cat = discord.utils.get(guild.categories, name=kind.category)
    if not cat:
        return await guild.create_category(kind.category, overwrites={guild.default_role: LOCKED, guild.me: KEEP})
    for ch in [cat, *(guild.get_channel(c) for c in sync_rooms(guild, kind))]:
        if ch and (ch.overwrites_for(guild.default_role).manage_channels is not False or ch.overwrites_for(guild.me).manage_channels is not True):
            try:
                await ch.set_permissions(guild.me, manage_channels=True)  # bot first, or the deny below locks the bot out
                await ch.set_permissions(guild.default_role, manage_channels=False)
            except discord.Forbidden:  # editing overwrites needs Manage Roles; @everyone can't manage channels by default anyway
                log.warning("bot lacks Manage Roles, so existing rooms in %s stay unlocked", guild.name)
                break
    return cat


@lockout.command(description="[Admin] Create N new rooms, numbered in order")
@is_admin
async def createroom(inter: discord.Interaction, number: app_commands.Range[int, 1, 25]):
    await create_rooms(inter, ROOMS, number)


@lockout.command(description="[Admin] Create N practice rooms (Extra Rooms): matches there don't count")
@is_admin
async def createextraroom(inter: discord.Interaction, number: app_commands.Range[int, 1, 25]):
    await create_rooms(inter, EXTRA, number)


async def create_rooms(inter: discord.Interaction, kind: Kind, number: int):
    await inter.response.defer(ephemeral=True)
    guild, p, t = inter.guild, kind.prefix, kind.table
    have = sync_rooms(guild, kind)
    if len(have) + number > MAX_ROOMS:
        return await inter.followup.send(
            f"Can't go over {MAX_ROOMS} {kind.label}s (Discord's per-category limit). There are {len(have)} now.", ephemeral=True
        )
    cat = await lock_rooms(guild, kind)
    start = max((room_number(guild.get_channel(c), kind) for c in have), default=0) + 1
    for i in range(start, start + number):
        ch = await cat.create_text_channel(f"{p}-{i}")
        db.add_rooms([ch.id], t)  # one at a time, so a Discord failure halfway doesn't orphan channels
    await inter.followup.send(f"Created {p}-{start} to {p}-{start + number - 1}. Total: {len(db.all_rooms(t))}.", ephemeral=True)


@lockout.command(description="[Admin] Delete one room by its number")
@app_commands.describe(number="The room's number: 5 deletes room-5")
@is_admin
async def removeroom(inter: discord.Interaction, number: app_commands.Range[int, 1, 999]):
    await delete_room(inter, ROOMS, number)


@lockout.command(description="[Admin] Delete one practice room by its number")
@app_commands.describe(number="The room's number: 5 deletes extra-5")
@is_admin
async def removeextraroom(inter: discord.Interaction, number: app_commands.Range[int, 1, 999]):
    await delete_room(inter, EXTRA, number)


async def delete_room(inter: discord.Interaction, kind: Kind, number: int):
    await inter.response.defer(ephemeral=True)  # answer first: Discord gives up after 3 seconds
    guild, name, t = inter.guild, f"{kind.prefix}-{number}", kind.table
    cid = next((c for c in sync_rooms(guild, kind) if room_number(guild.get_channel(c), kind) == number), None)
    if not cid:
        return await inter.followup.send(f"There is no {name}. Nothing deleted.", ephemeral=True)
    if busy(cid):
        return await inter.followup.send(f"{name} has a match. Run /lockout endmatch there first.", ephemeral=True)
    db.remove_rooms([cid], t)  # before the next await, so nobody can start a challenge in a room being deleted
    await guild.get_channel(cid).delete()
    if not db.all_rooms(t) and (cat := discord.utils.get(guild.categories, name=kind.category)):
        await cat.delete()
    await inter.followup.send(f"Deleted {name}. {len(db.all_rooms(t))} left.", ephemeral=True)


@lockout.command(description="[Admin] Cancel the match in a room (default: this channel)")
@is_admin
async def endmatch(inter: discord.Interaction, room: discord.TextChannel | None = None):
    room = room or inter.channel
    m = matches.pop(room.id, None)
    view = pending.pop(room.id, None)
    if m and m.task:
        m.task.cancel()
    db.drop_active(room.id)
    if not (m or view):
        return await inter.response.send_message(f"Nothing running in {room.mention}.", ephemeral=True)
    await inter.response.send_message(f"Cancelled the match in {room.mention}. Room is free.", ephemeral=True)
    if view:
        view.stop()
        if view.message:
            try:
                await view.message.edit(content="Challenge cancelled by organizer.", view=None)
            except discord.HTTPException:
                pass
    await room.send("🛑 Match cancelled by organizer.")


@lockout.command(description="[Admin] Create the private technical channel for admin commands")
@admin_anywhere
async def technical(inter: discord.Interaction):
    if tech := technical_channel(inter.guild):
        return await inter.response.send_message(f"Already exists: {tech.mention}", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    guild = inter.guild
    rw = discord.PermissionOverwrite(view_channel=True, send_messages=True)  # only bits the bot itself holds
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True),
        inter.user: rw,
    }
    for uid in admin_ids(guild):
        try:
            overwrites[await guild.fetch_member(uid)] = rw
        except discord.NotFound:
            pass
    ch = await guild.create_text_channel("technical", overwrites=overwrites, topic="Admin only: run /lockout admin commands here")
    db.set_config("technical", ch.id)
    await ch.send("🔧 Admin channel ready. Run `/lockout createroom`, `/lockout health` and the other admin commands here.")
    await inter.followup.send(f"Created {ch.mention}. Only you (and other admins) can see it.", ephemeral=True)


@lockout.command(description="[Admin] Register a player without verification")
@is_admin
async def forceregister(inter: discord.Interaction, member: discord.Member,
                        handle: app_commands.Range[str, 3, 24], name: app_commands.Range[str, 1, 40]):
    name = clean_name(name)
    if member.bot or not name or not valid_handle(handle):
        return await inter.response.send_message("Need a human member, a valid Codeforces handle and a name.", ephemeral=True)
    await inter.response.defer(ephemeral=True)
    try:
        canon = (await cf.user(handle))["handle"]
    except CFError as e:
        return await inter.followup.send(f"Codeforces says: {e}", ephemeral=True)
    taken = db.by_handle(canon)
    if taken and taken["discord_id"] != member.id:
        return await inter.followup.send(f"`{canon}` is already registered to <@{taken['discord_id']}>. Unregister them first.", ephemeral=True)
    db.save_user(member.id, canon, name)
    await inter.followup.send(f"Registered {member.mention} as {safe(name)} ({canon}).", ephemeral=True)


@lockout.command(description="[Admin] Remove a player's registration")
@is_admin
async def unregister(inter: discord.Interaction, member: discord.Member):
    if not db.user(member.id):
        return await inter.response.send_message(f"{member.mention} wasn't registered.", ephemeral=True)
    db.delete_user(member.id)
    await inter.response.send_message("Removed.", ephemeral=True)


async def send_table(inter: discord.Interaction, title: str, rows: list[str], filename: str):
    """A code block (no pings, no markdown); attached as a file when it won't fit in one message."""
    body = "\n".join(r.replace("`", "'") for r in rows)
    text = f"{title}\n```\n{body}\n```"
    if len(text) <= MAX_MESSAGE:
        return await inter.response.send_message(text, ephemeral=True)
    await inter.response.send_message(
        f"{title} (too long for one message, see the file)", file=discord.File(io.BytesIO(body.encode()), filename), ephemeral=True
    )


@lockout.command(description="[Admin] List every registered player and their Codeforces handle")
@is_admin
async def players(inter: discord.Interaction):
    users = db.all_users()
    if not users:
        return await inter.response.send_message("No players registered yet.", ephemeral=True)
    w = max(len(u["real_name"]) for u in users)
    rows = [f"{'#':>3}  {'Name':<{w}}  Codeforces"] + [
        f"{n:>3}  {u['real_name']:<{w}}  {u['handle']}" for n, u in enumerate(users, 1)
    ]
    await send_table(inter, f"**👥 {len(users)} registered player(s)**", rows, "players.txt")


@lockout.command(name="matches", description="[Admin] List every finished match and its winner")
@is_admin
async def match_history(inter: discord.Interaction):
    results = db.all_results()
    if not results:
        return await inter.response.send_message("No matches finished yet.", ephemeral=True)
    names = {u["handle"].lower(): u["real_name"] for u in db.all_users()}
    who = lambda h: f"{names[h.lower()]} ({h})" if h.lower() in names else h  # noqa: E731
    rows = [
        f"{n:>3}. {time.strftime('%d %b %H:%M', time.localtime(r['finished_at']))}  "
        f"{who(r['p1'])} {r['score1']}-{r['score2']} {who(r['p2'])}  ->  winner: {who(r['winner'])}"
        + (" (tie-breaker)" if r["tiebreak"] else "")
        for n, r in enumerate(results, 1)
    ]
    await send_table(inter, f"**🏆 {len(results)} finished match(es)**", rows, "matches.txt")


@lockout.command(description="[Admin] Health check and live match status")
@is_admin
async def health(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    t = time.perf_counter()
    try:
        await cf.user("tourist")
        cf_state = f"🟢 OK ({(time.perf_counter() - t) * 1000:.0f} ms)"
    except Exception as e:
        cf_state = f"🔴 {e}"
    try:
        n_users = db.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        db_state = f"🟢 OK ({n_users} players registered)"
    except Exception as e:
        db_state = f"🔴 {e}"
    ids = db.all_rooms()
    gone = [c for c in ids if not inter.guild.get_channel(c)]
    perms = inter.guild.me.guild_permissions
    lacks = [n for n in ("manage_channels", "view_channel", "send_messages", "embed_links") if not getattr(perms, n)]
    up = int(time.time() - bot.started)
    live = []
    for m in matches.values():
        a, b = m.scores()
        left = "starting" if not m.start else f"{m.left()} left" if m.seconds_left() else "time's up, finishing"
        tag = " · practice" if m.practice else ""
        live.append(f"{m.channel.mention}: {safe(m.players[0]['handle'])} {a}–{b} {safe(m.players[1]['handle'])} · {left}{tag}")
    lines = [
        f"**Discord:** 🟢 {bot.latency * 1000:.0f} ms · up {up // 3600}h {up % 3600 // 60}m",
        f"**Codeforces API:** {cf_state}",
        f"**Database:** {db_state}",
        f"**Rooms:** {len(ids)} total · {len([c for c in ids if busy(c)])} in use" + (f" · ⚠️ {len(gone)} deleted channel(s), re-run createroom" if gone else ""),
        f"**Extra rooms (practice):** {len(db.all_rooms(EXTRA.table))}",
        "**Bot permissions:** " + ("🟢 all good" if not lacks else f"🔴 missing {', '.join(lacks)}"),
        "**Live matches:** " + ("none" if not live else "\n" + "\n".join(live)),
    ]
    await inter.followup.send(clip("\n".join(lines)), ephemeral=True)


if __name__ == "__main__":
    bot.run(os.environ["DISCORD_TOKEN"])
