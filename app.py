import math
import os
import random
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import (
    Flask, abort, flash, g, redirect, render_template,
    request, session, url_for,
)

APP_DIR = Path(__file__).parent
DB_PATH = Path(os.environ.get("DB_PATH", APP_DIR / "experience.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

CUPS = [f"{L}{n}" for L in "ABCDE" for n in range(1, 21)]

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cups (
    cup_id TEXT PRIMARY KEY,
    coke_type TEXT NOT NULL CHECK (coke_type IN ('normal', 'zero'))
);
CREATE TABLE IF NOT EXISTS participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id INTEGER NOT NULL REFERENCES participants(id),
    cup_id TEXT NOT NULL UNIQUE REFERENCES cups(cup_id),
    position INTEGER NOT NULL,
    guess TEXT CHECK (guess IN ('normal', 'zero')),
    confidence INTEGER CHECK (confidence BETWEEN 1 AND 5),
    voted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_assignments_participant
    ON assignments(participant_id, position);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
        # Migrate: ensure participants.name uses COLLATE NOCASE
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='participants'"
        ).fetchone()
        if row and "NOCASE" not in (row[0] or "").upper():
            try:
                conn.executescript("""
                    BEGIN;
                    ALTER TABLE participants RENAME TO participants_old;
                    CREATE TABLE participants (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                        created_at TEXT NOT NULL
                    );
                    INSERT INTO participants (id, name, created_at)
                        SELECT id, name, created_at FROM participants_old;
                    DROP TABLE participants_old;
                    COMMIT;
                """)
            except sqlite3.Error as e:
                print(f"Migration skipped: {e}")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_config(key, default=None):
    row = get_db().execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_config(key, value):
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
        (key, str(value)),
    )
    db.commit()


def is_setup_done():
    return get_config("setup_done") == "1"


def is_results_published():
    return get_config("results_published") == "1"


@app.context_processor
def inject_globals():
    try:
        return {"results_published": is_results_published()}
    except sqlite3.OperationalError:
        return {"results_published": False}


def compute_stats():
    db = get_db()
    per = db.execute("""
        SELECT p.name,
               COUNT(a.id) total,
               SUM(CASE WHEN a.voted_at IS NOT NULL THEN 1 ELSE 0 END) voted,
               SUM(CASE WHEN a.guess = c.coke_type THEN 1 ELSE 0 END) correct
        FROM participants p
        LEFT JOIN assignments a ON a.participant_id = p.id
        LEFT JOIN cups c ON c.cup_id = a.cup_id
        GROUP BY p.id, p.name
    """).fetchall()
    participants = []
    for r in per:
        voted = r["voted"] or 0
        correct = r["correct"] or 0
        participants.append({
            "name": r["name"],
            "voted": voted,
            "correct": correct,
            "accuracy": (correct / voted * 100) if voted else 0,
        })
    participants.sort(key=lambda x: (-x["accuracy"], -x["correct"], x["name"].lower()))

    conf_rows = db.execute("""
        SELECT a.confidence conf,
               COUNT(*) total,
               SUM(CASE WHEN a.guess = c.coke_type THEN 1 ELSE 0 END) correct
        FROM assignments a
        JOIN cups c ON c.cup_id = a.cup_id
        WHERE a.voted_at IS NOT NULL
        GROUP BY a.confidence
    """).fetchall()
    by_conf = []
    conf_map = {r["conf"]: r for r in conf_rows}
    for level in range(1, 6):
        r = conf_map.get(level)
        total = r["total"] if r else 0
        correct = r["correct"] if r else 0
        by_conf.append({
            "level": level,
            "total": total,
            "correct": correct,
            "accuracy": (correct / total * 100) if total else 0,
        })

    confusion_rows = db.execute("""
        SELECT c.coke_type truth, a.guess, COUNT(*) n
        FROM assignments a
        JOIN cups c ON c.cup_id = a.cup_id
        WHERE a.voted_at IS NOT NULL
        GROUP BY c.coke_type, a.guess
    """).fetchall()
    confusion = {
        ("normal", "normal"): 0, ("normal", "zero"): 0,
        ("zero", "normal"): 0,  ("zero", "zero"): 0,
    }
    for r in confusion_rows:
        confusion[(r["truth"], r["guess"])] = r["n"]

    totals = db.execute("""
        SELECT COUNT(*) voted,
               SUM(CASE WHEN a.guess = c.coke_type THEN 1 ELSE 0 END) correct
        FROM assignments a
        JOIN cups c ON c.cup_id = a.cup_id
        WHERE a.voted_at IS NOT NULL
    """).fetchone()
    total_voted = totals["voted"] or 0
    total_correct = totals["correct"] or 0
    accuracy = (total_correct / total_voted * 100) if total_voted else 0

    # One-sided p-value vs. random 50/50 (normal approximation)
    if total_voted > 0:
        mean = total_voted * 0.5
        std = math.sqrt(total_voted * 0.25)
        z = (total_correct - mean) / std if std > 0 else 0
        p_value = 0.5 * math.erfc(z / math.sqrt(2))
    else:
        z = 0
        p_value = None

    return {
        "participants": participants,
        "by_confidence": by_conf,
        "confusion": {
            "true_normal_guess_normal": confusion[("normal", "normal")],
            "true_normal_guess_zero":   confusion[("normal", "zero")],
            "true_zero_guess_normal":   confusion[("zero", "normal")],
            "true_zero_guess_zero":     confusion[("zero", "zero")],
        },
        "total_voted": total_voted,
        "total_correct": total_correct,
        "total_incorrect": total_voted - total_correct,
        "accuracy": accuracy,
        "z": z,
        "p_value": p_value,
    }


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


# ---------- PARTICIPANT ROUTES ----------

@app.route("/")
def home():
    return render_template("home.html", setup_done=is_setup_done())


@app.route("/start", methods=["POST"])
def start():
    if not is_setup_done():
        return redirect(url_for("home"))
    name = request.form.get("name", "").strip()
    if not name:
        flash("Merci d'entrer un prénom.", "error")
        return redirect(url_for("home"))
    db = get_db()
    row = db.execute(
        "SELECT id, name FROM participants WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    if row:
        participant_id = row["id"]
        name = row["name"]  # keep the originally-registered spelling
    else:
        n = int(get_config("cups_per_person", "10"))
        n_normal = n // 2
        n_zero = n - n_normal
        avail_n = db.execute("""
            SELECT c.cup_id FROM cups c
            LEFT JOIN assignments a ON a.cup_id = c.cup_id
            WHERE a.id IS NULL AND c.coke_type = 'normal'
        """).fetchall()
        avail_z = db.execute("""
            SELECT c.cup_id FROM cups c
            LEFT JOIN assignments a ON a.cup_id = c.cup_id
            WHERE a.id IS NULL AND c.coke_type = 'zero'
        """).fetchall()
        if len(avail_n) < n_normal or len(avail_z) < n_zero:
            # Fallback: take whatever is left, mixed
            all_avail = [r["cup_id"] for r in avail_n] + [r["cup_id"] for r in avail_z]
            if len(all_avail) < n:
                flash(
                    "Plus assez de gobelets disponibles pour un nouveau participant.",
                    "error",
                )
                return redirect(url_for("home"))
            picks = random.sample(all_avail, n)
        else:
            picks = (
                random.sample([r["cup_id"] for r in avail_n], n_normal)
                + random.sample([r["cup_id"] for r in avail_z], n_zero)
            )
        random.shuffle(picks)
        cur = db.execute(
            "INSERT INTO participants (name, created_at) VALUES (?, ?)",
            (name, now_iso()),
        )
        participant_id = cur.lastrowid
        for i, cup in enumerate(picks, start=1):
            db.execute(
                "INSERT INTO assignments (participant_id, cup_id, position)"
                " VALUES (?, ?, ?)",
                (participant_id, cup, i),
            )
        db.commit()
    session["participant_id"] = participant_id
    session["participant_name"] = name
    return redirect(url_for("taste"))


@app.route("/taste")
def taste():
    pid = session.get("participant_id")
    if not pid:
        return redirect(url_for("home"))
    db = get_db()
    row = db.execute("""
        SELECT * FROM assignments
        WHERE participant_id = ? AND voted_at IS NULL
        ORDER BY position ASC LIMIT 1
    """, (pid,)).fetchone()
    if not row:
        return redirect(url_for("done"))
    stats = db.execute("""
        SELECT COUNT(*) total,
               SUM(CASE WHEN voted_at IS NOT NULL THEN 1 ELSE 0 END) voted
        FROM assignments WHERE participant_id = ?
    """, (pid,)).fetchone()
    return render_template(
        "taste.html",
        assignment=row,
        name=session.get("participant_name"),
        total=stats["total"],
        voted=stats["voted"] or 0,
    )


@app.route("/vote/<int:assignment_id>", methods=["POST"])
def vote(assignment_id):
    pid = session.get("participant_id")
    if not pid:
        return redirect(url_for("home"))
    db = get_db()
    row = db.execute(
        "SELECT * FROM assignments WHERE id = ? AND participant_id = ?",
        (assignment_id, pid),
    ).fetchone()
    if not row:
        abort(404)
    guess = request.form.get("guess")
    conf = request.form.get("confidence")
    if guess not in ("normal", "zero") or conf not in ("1", "2", "3", "4", "5"):
        flash("Vote invalide.", "error")
        return redirect(url_for("taste"))
    db.execute(
        "UPDATE assignments SET guess = ?, confidence = ?, voted_at = ? WHERE id = ?",
        (guess, int(conf), now_iso(), assignment_id),
    )
    db.commit()
    return redirect(url_for("taste"))


@app.route("/done")
def done():
    pid = session.get("participant_id")
    if not pid:
        return redirect(url_for("home"))
    db = get_db()
    stats = db.execute("""
        SELECT COUNT(*) total,
               SUM(CASE WHEN voted_at IS NOT NULL THEN 1 ELSE 0 END) voted
        FROM assignments WHERE participant_id = ?
    """, (pid,)).fetchone()
    return render_template(
        "done.html",
        name=session.get("participant_name"),
        total=stats["total"],
        voted=stats["voted"] or 0,
    )


@app.route("/results")
def results():
    if not is_results_published():
        db = get_db()
        stats = db.execute("""
            SELECT COUNT(*) total,
                   SUM(CASE WHEN voted_at IS NOT NULL THEN 1 ELSE 0 END) voted
            FROM assignments
        """).fetchone()
        return render_template(
            "results_pending.html",
            total=stats["total"] or 0,
            voted=stats["voted"] or 0,
        )
    return render_template("results.html", stats=compute_stats())


@app.route("/logout")
def logout():
    session.pop("participant_id", None)
    session.pop("participant_name", None)
    return redirect(url_for("home"))


# ---------- ADMIN ROUTES ----------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(request.args.get("next") or url_for("admin"))
        flash("Mot de passe incorrect", "error")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("home"))


@app.route("/admin")
@admin_required
def admin():
    db = get_db()
    n_participants = db.execute(
        "SELECT COUNT(*) c FROM participants"
    ).fetchone()["c"]
    n_votes = db.execute(
        "SELECT COUNT(*) c FROM assignments WHERE voted_at IS NOT NULL"
    ).fetchone()["c"]
    n_assignments = db.execute(
        "SELECT COUNT(*) c FROM assignments"
    ).fetchone()["c"]
    n_cups = db.execute("SELECT COUNT(*) c FROM cups").fetchone()["c"]
    n_normal = db.execute(
        "SELECT COUNT(*) c FROM cups WHERE coke_type = 'normal'"
    ).fetchone()["c"]
    return render_template(
        "admin.html",
        setup_done=is_setup_done(),
        cups_per_person=get_config("cups_per_person", "10"),
        n_participants=n_participants,
        n_votes=n_votes,
        n_assignments=n_assignments,
        n_cups=n_cups,
        n_normal=n_normal,
        n_zero=n_cups - n_normal,
    )


@app.route("/admin/setup", methods=["GET", "POST"])
@admin_required
def admin_setup():
    if request.method == "POST":
        try:
            cups_per_person = int(request.form.get("cups_per_person", "10"))
            n_normal = int(request.form.get("n_normal", "50"))
        except ValueError:
            flash("Valeurs invalides", "error")
            return redirect(url_for("admin_setup"))
        if not (1 <= cups_per_person <= 100):
            flash("Nombre de gobelets par personne invalide (1–100).", "error")
            return redirect(url_for("admin_setup"))
        if not (0 <= n_normal <= 100):
            flash("Nombre de Coca normal invalide (0–100).", "error")
            return redirect(url_for("admin_setup"))
        db = get_db()
        db.execute("DELETE FROM assignments")
        db.execute("DELETE FROM participants")
        db.execute("DELETE FROM cups")
        shuffled = CUPS.copy()
        random.shuffle(shuffled)
        normals = set(shuffled[:n_normal])
        for c in CUPS:
            db.execute(
                "INSERT INTO cups (cup_id, coke_type) VALUES (?, ?)",
                (c, "normal" if c in normals else "zero"),
            )
        set_config("cups_per_person", cups_per_person)
        set_config("n_normal", n_normal)
        set_config("setup_done", "1")
        set_config("results_published", "0")
        db.commit()
        flash("Expérience initialisée. Les gobelets sont assignés.", "success")
        return redirect(url_for("admin"))
    return render_template(
        "admin_setup.html",
        cups_per_person=get_config("cups_per_person", "10"),
        n_normal=get_config("n_normal", "50"),
        setup_done=is_setup_done(),
    )


@app.route("/admin/prepare")
@app.route("/admin/prepare/<int:i>")
@admin_required
def admin_prepare(i=1):
    if not is_setup_done():
        flash("Configure d'abord l'expérience.", "error")
        return redirect(url_for("admin"))
    total = len(CUPS)
    if i < 1 or i > total:
        return redirect(url_for("admin_prepare", i=1))
    cup_id = CUPS[i - 1]
    row = get_db().execute(
        "SELECT coke_type FROM cups WHERE cup_id = ?", (cup_id,)
    ).fetchone()
    return render_template(
        "admin_prepare.html",
        cup_id=cup_id,
        coke_type=row["coke_type"] if row else None,
        i=i,
        total=total,
        prev_i=i - 1 if i > 1 else None,
        next_i=i + 1 if i < total else None,
    )


@app.route("/admin/cups")
@admin_required
def admin_cups():
    db = get_db()
    rows = db.execute(
        "SELECT cup_id, coke_type FROM cups ORDER BY cup_id"
    ).fetchall()
    by_letter = {}
    for r in rows:
        by_letter.setdefault(r["cup_id"][0], []).append(r)
    for L in by_letter:
        by_letter[L].sort(key=lambda r: int(r["cup_id"][1:]))
    return render_template("admin_cups.html", by_letter=by_letter)


@app.route("/admin/results")
@admin_required
def admin_results():
    db = get_db()
    rows = db.execute("""
        SELECT p.id, p.name,
               COUNT(a.id) total,
               SUM(CASE WHEN a.voted_at IS NOT NULL THEN 1 ELSE 0 END) voted,
               SUM(CASE WHEN a.guess = c.coke_type THEN 1 ELSE 0 END) correct,
               SUM(CASE WHEN a.guess = c.coke_type
                        THEN a.confidence ELSE 0 END) score_pos,
               SUM(CASE WHEN a.voted_at IS NOT NULL AND a.guess != c.coke_type
                        THEN a.confidence ELSE 0 END) score_neg
        FROM participants p
        LEFT JOIN assignments a ON a.participant_id = p.id
        LEFT JOIN cups c ON c.cup_id = a.cup_id
        GROUP BY p.id, p.name
    """).fetchall()
    ranking = []
    for r in rows:
        correct = r["correct"] or 0
        voted = r["voted"] or 0
        total = r["total"] or 0
        weighted = (r["score_pos"] or 0) - (r["score_neg"] or 0)
        accuracy = (correct / voted * 100) if voted else 0
        ranking.append({
            "name": r["name"],
            "correct": correct,
            "voted": voted,
            "total": total,
            "accuracy": accuracy,
            "weighted": weighted,
        })
    ranking.sort(
        key=lambda x: (-x["correct"], -x["weighted"], x["name"].lower())
    )
    details = db.execute("""
        SELECT p.name, a.cup_id, c.coke_type, a.guess, a.confidence, a.voted_at,
               a.position
        FROM assignments a
        JOIN participants p ON p.id = a.participant_id
        JOIN cups c ON c.cup_id = a.cup_id
        ORDER BY p.name, a.position
    """).fetchall()
    details_by_name = {}
    for d in details:
        details_by_name.setdefault(d["name"], []).append(d)
    return render_template(
        "admin_results.html",
        ranking=ranking,
        details_by_name=details_by_name,
    )


@app.route("/admin/publish", methods=["POST"])
@admin_required
def admin_publish():
    action = request.form.get("action", "publish")
    set_config("results_published", "1" if action == "publish" else "0")
    flash(
        "Résultats publiés. Tout le monde peut les voir sur /results."
        if action == "publish"
        else "Résultats retirés du public.",
        "success",
    )
    return redirect(url_for("admin"))


@app.route("/admin/reset", methods=["POST"])
@admin_required
def admin_reset():
    db = get_db()
    db.execute("DELETE FROM assignments")
    db.execute("DELETE FROM participants")
    db.execute("DELETE FROM cups")
    db.execute("DELETE FROM config")
    db.commit()
    flash("Tout a été réinitialisé.", "success")
    return redirect(url_for("admin"))


init_db()


def _print_lan_urls(port):
    import socket
    print(f"\n  Local:   http://127.0.0.1:{port}")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        print(f"  LAN:     http://{ip}:{port}   (share with people on the same Wi-Fi)")
    except Exception:
        pass
    print()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    _print_lan_urls(port)
    app.run(host="0.0.0.0", port=port, debug=True)
