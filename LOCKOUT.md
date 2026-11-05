# Lockout: Context & Rules

A reference for what Lockout is, how a match works, and every rule that applies in our tournament.
(This is about the game and its rules, not about how the bot is built.)

---

## 1. What is Lockout?

Lockout is a **1v1 competitive-programming race** played on Codeforces problems.

- Two players get the **same set of 5 problems** at the same moment.
- Each problem can be **claimed by only one player**: whoever gets an **accepted (AC) submission first** takes it. The other player is **locked out** of that problem. Solving it later earns them nothing.
- Each problem is worth points. The player with **more points when time runs out wins**.

The name comes from that mechanic: once a problem is taken, the opponent is locked out of it.

Because of this, Lockout is not only "who solves more". It is also **who solves first** and **which problems you pick** (speed, strategy, and nerve all matter).

---

## 2. Quick summary of our rules

| Item | Rule |
|---|---|
| Players | 2 (1v1) |
| Problems | 5 per match, Codeforces problems |
| Ratings | Chosen by the challenger: base rating `R` (multiple of 100, 800 to 3100). Problems are `R`, `R+100`, `R+200`, `R+300`, `R+400` |
| Points | Problem 1 = **100**, Problem 2 = **200**, Problem 3 = **300**, Problem 4 = **400**, Problem 5 = **500** (max 1500 total) |
| Claiming | **First accepted submission** on Codeforces takes the problem |
| Wrong submissions | **No penalty** |
| Match length | Set by the challenger (`minutes`), default **15 minutes** |
| Winner | Most points when time ends |
| Tie | Ends in a **draw** directly |
| Result | Winner **advances**, loser is **knocked out** (in competition rooms) |

---

## 3. Problems

- 5 problems per match, in **increasing difficulty**: base rating, +100, +200, +300, +400.
  - Example: base 1200 gives problems rated 1200, 1300, 1400, 1500, 1600.
- Problems are drawn **randomly** from the Codeforces problemset, and each has a **rating**.
- **Fairness filter:** a problem is only used if **neither player has ever submitted to it** (any verdict). Problems are also checked by **name**, so the Div. 1 / Div. 2 twin of a problem you've seen is excluded too.
- **Interactive problems are never used.**
- No problem repeats within a match.
- Problems are shown on the board with their points, rating, and a link.

### Points per problem

| # | Points | Rating (base R) |
|---|---|---|
| 1 | 100 | R |
| 2 | 200 | R + 100 |
| 3 | 300 | R + 200 |
| 4 | 400 | R + 300 |
| 5 | 500 | R + 400 |

Harder problems are worth more, so a single late hard solve can beat several easy ones.

---

## 4. How scoring works

- A problem belongs to whoever has the **earliest accepted submission** on it **during the match window**.
- Only submissions made **between match start and match end** count. Anything submitted before the start or after time is up is ignored.
- Submission is judged by **Codeforces timestamps**, not Discord message order.
- If you solve a problem the opponent already took, you get **0 points** for it.
- **Wrong answers, runtime errors, TLEs, etc. cost nothing.** No penalty time. Only AC counts.
- Both players can keep working on a problem until someone gets AC. You cannot "see" whether the opponent is stuck.
- The live scoreboard shows `PlayerA (X points) vs (Y points) PlayerB`, with each solved problem marked as owned by whoever took it.
- Every time a problem is taken, the room gets an "update in standings" message announcing who solved what and the new score.

### Strategy notes

- Because the first AC wins the problem, **speed on a problem the opponent is also attempting matters**.
- Often a good plan: take the cheap problems fast, or go straight for a high-value problem the opponent can't solve.
- You don't need to solve in order. Pick whichever problem you can finish first.
- Taking a problem also denies it to your opponent, so a 100-pointer is worth more than 100 if the opponent was about to solve it.

---

## 5. How a match happens (step by step)

1. **Register (one time).** Link your Codeforces handle and real name. You must prove you own the handle (see section 8).
2. **Go to a free room.** Matches are played in dedicated room channels (`room-1`, `room-2`, …). One match per room at a time.
3. **Challenge.** One player challenges another in the room, picking the **rating** and optionally the **number of minutes**.
4. **Accept.** The opponent presses **Accept** (or declines). The challenge **expires after 2 minutes** if ignored. The challenger can withdraw it.
5. **Setup.** The system picks 5 problems that neither player has seen. Both players are tagged.
6. **Countdown.** Problems drop after a **10 second** countdown ("Get ready!").
7. **Match starts.** A board shows the 5 problems, points, ratings, scores, and a **live time-left** counter.
8. **Play.** Solve and submit on Codeforces **as normal, on your own account**. Nothing is submitted through Discord. The room checks Codeforces every few seconds and announces solves.
9. **Time's up.** The board switches to "Time's up".
10. **Judging grace.** A short grace period (20 seconds), plus up to 3 more minutes if a submission made in time is still sitting in the judging queue, so last-second submissions are judged fairly.
11. **Result.** Higher score wins. If level, the match ends in a draw.
12. **Announcement.** The room announces the winner and final score. The winner advances.

### The challenge

- Rating must be a **multiple of 100** and between **800 and 3100**.
- Match length is optional. If not given, the default (15 minutes) applies.
- Both players must be **registered**.
- A player can only be in **one match at a time**, and a room only hosts **one match at a time**.

---

## 6. Ties and Draws

- If both players have the **same score** when time and judging end, the match ends directly in a **draw**.
- There is no tie-breaker problem: equal points at the end convert immediately to a draw.
- A 0-0 match also ends in a draw.

---

## 7. Competition rooms vs. practice rooms

| | Competition rooms | Extra (practice) rooms |
|---|---|---|
| Purpose | Real tournament matches | Practice and warm-up |
| Result saved | Yes | **No** |
| Winner | Advances | Just a "practice" result |
| Loser | Knocked out | Nothing happens |
| Rules and scoring | Same | Same |

Practice matches are labelled **PRACTICE** when they start and say "doesn't count for the competition" at the end.

---

## 8. Registration and handle verification

To stop people claiming someone else's account:

1. Run the register command with your **Codeforces handle** and **real name**.
2. You are given a specific problem to submit a **deliberately failing submission** (a **compile error**) to on Codeforces, within **5 minutes**.
3. The system checks your recent submissions for it. If it sees it, your handle is verified and linked to your Discord account.
4. If the 5 minutes expire, just register again.

Admins can also register or remove a player manually.

Important: **your Codeforces handle is how your solves are tracked**. Use the registered account for everything during a match.

---

## 9. Fair play rules

- **Solve on your own.** No help from other people, no sharing solutions, no looking up editorials or solutions during the match.
- Use only your own registered account. Submitting from another account will not be counted for you.
- Don't use bots, auto-solvers or AI to solve problems. (Standard Codeforces and competition norms apply.)
- Only **Codeforces submissions made during the match window** count.
- Don't open the problems early. They are released after the countdown.
- Organizer decisions are final on disputes, crashes, or Codeforces outages.

> Items in this section are general competitive-programming conduct norms. Adjust them if the organizers set stricter or looser rules.

---

## 10. Edge cases and what happens

| Situation | What happens |
|---|---|
| Both solve a problem | Whoever's **accepted submission has the earlier Codeforces timestamp** owns it |
| Submission in the last seconds | Counted if it was made before time ended, even if judging finishes slightly after |
| Submission still "in queue" at the end | Bot waits up to ~3 extra minutes for the verdict |
| Submission made after time ends | Ignored |
| Many wrong submissions | No effect (no penalty) |
| Opponent solved the problem already | You can still submit, but it gives you no points |
| Score level at the end | Converts directly to a **draw** |
| Codeforces down or unreachable | Retries several times; if it still fails, organizers decide manually |
| Challenge ignored | Expires after 2 minutes, room is freed |
| Organizer cancels a match | Match ends, the room is free again, no result is recorded |
| Bot restarts mid-match | The match resumes with the same problems and clock |
| Problem pool at a rating exhausted | Match can't start, and the room is freed (very high or very low ratings have fewer problems) |

---

## 11. Tournament structure

- Each competition match is **knockout**: the winner advances, the loser is out.
- Results (players, scores, winner, tie or not) are saved for competition rooms.
- Admins can list all finished matches and registered players.
- The organizers decide the bracket, who plays whom, and the order. The system only runs and records the matches.

---

## 12. Commands cheat sheet (player side)

| Command | What it does |
|---|---|
| `/lockout help` | How to play |
| `/lockout register handle name` | Link your Codeforces handle (one time) |
| `/lockout rooms` | Show which rooms are free |
| `/lockout challenge @opponent rating [minutes]` | Challenge someone in the current room |
| `/lockout score` | Live score of the match in this room |

### Admin commands

| Command | What it does |
|---|---|
| `/lockout createroom N` / `removeroom N` | Add or delete competition rooms |
| `/lockout createextraroom N` / `removeextraroom N` | Add or delete practice rooms |
| `/lockout endmatch [room]` | Cancel a match or open challenge |
| `/lockout technical` | Create the private admin channel |
| `/lockout forceregister @player handle name` | Register without verification |
| `/lockout unregister @player` | Remove a registration |
| `/lockout players` | List registered players and handles |
| `/lockout matches` | List finished matches and winners |
| `/lockout health` | Bot status and live matches |

---

## 13. Glossary

- **AC (Accepted):** a submission that passes all tests. The only verdict that scores.
- **Lockout:** once someone gets AC on a problem, the opponent cannot score from it.
- **Base rating:** the rating the challenger picks. The 5 problems go up from there in steps of 100.
- **Board:** the live message showing problems, points, ratings, owners, and time left.
- **Draw:** when scores are level at the end, the match concludes directly as a draw.
- **Advance / knocked out:** the winner proceeds in the tournament, the loser is eliminated.
- **Practice room:** a room where matches don't count.
- **Judging queue:** Codeforces testing a submission. The system waits for these at the end.
