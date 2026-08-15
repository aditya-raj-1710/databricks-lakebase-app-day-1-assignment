"""
Databricks App: Lakebase-powered support ticket system.

- Serves a small Flask API + single-page UI
- Reads/writes support tickets and their messages to Lakebase
  (Databricks-managed Postgres) via lakebase.py

Run locally:
    python app.py
Deploy as a Databricks App using app.yaml.
"""

import logging
import os

from databricks.sdk import WorkspaceClient
from flask import Flask, jsonify, render_template, request

import lakebase

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("support-ticket-app")

app = Flask(__name__)
_w = WorkspaceClient()

TICKETS_TABLE = os.environ.get("TICKETS_TABLE_NAME", "tickets")
TICKET_MESSAGES_TABLE = os.environ.get("TICKET_MESSAGES_TABLE_NAME", "ticket_messages")

VALID_STATUSES = ("open", "in_progress", "resolved")


def ensure_tickets_tables():
    """Create the tickets/ticket_messages tables in Lakebase if they don't
    exist yet, and seed sample data on first run so the app has something
    to display immediately after deploy."""
    lakebase.run_write(
        f"""
        CREATE TABLE IF NOT EXISTS {TICKETS_TABLE} (
            ticket_id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_by TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    lakebase.run_write(
        f"""
        CREATE TABLE IF NOT EXISTS {TICKET_MESSAGES_TABLE} (
            message_id SERIAL PRIMARY KEY,
            ticket_id INTEGER NOT NULL REFERENCES {TICKETS_TABLE}(ticket_id) ON DELETE CASCADE,
            message_text TEXT NOT NULL,
            author TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    _seed_sample_data_if_empty()


def _seed_sample_data_if_empty():
    """Insert sample tickets/messages the first time the app runs against an
    empty database, so the assignment's data requirements are met without a
    separate manual step."""
    existing = lakebase.run_query(f"SELECT COUNT(*) AS count FROM {TICKETS_TABLE}")
    if existing and existing[0]["count"] > 0:
        return

    logger.info("Seeding sample tickets and messages into Lakebase")

    sample_tickets = [
        ("Cannot log into dashboard", "open", "alice@example.com"),
        ("Export button not working", "in_progress", "bob@example.com"),
        ("Feature request: dark mode", "resolved", "carol@example.com"),
    ]
    ticket_ids = []
    for title, status, created_by in sample_tickets:
        rows = lakebase.run_query(
            f"""
            INSERT INTO {TICKETS_TABLE} (title, status, created_by)
            VALUES (%s, %s, %s)
            RETURNING ticket_id
            """,
            (title, status, created_by),
        )
        ticket_ids.append(rows[0]["ticket_id"])

    sample_messages = [
        (ticket_ids[0], "I get an error on the login page.", "alice@example.com"),
        (ticket_ids[0], "Can you share a screenshot?", "support@example.com"),
        (ticket_ids[1], "The export button does nothing when clicked.", "bob@example.com"),
        (ticket_ids[1], "We're looking into it, thanks for reporting.", "support@example.com"),
        (ticket_ids[2], "Would love a dark mode option.", "carol@example.com"),
        (ticket_ids[2], "Dark mode has been shipped, closing this out.", "support@example.com"),
    ]
    for ticket_id, message_text, author in sample_messages:
        lakebase.run_write(
            f"""
            INSERT INTO {TICKET_MESSAGES_TABLE} (ticket_id, message_text, author)
            VALUES (%s, %s, %s)
            """,
            (ticket_id, message_text, author),
        )


def _current_user_email() -> str:
    """
    Resolve the current user's email so new tickets/messages can be
    attributed to whoever is using the app.

    Databricks Apps inject the logged-in user's identity via the
    X-Forwarded-Email header on every request. Fall back to the Databricks
    SDK's current_user API for local development where that header isn't set.
    """
    header_email = request.headers.get("X-Forwarded-Email")
    if header_email:
        return header_email
    return _w.current_user.me().user_name


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.errorhandler(Exception)
def handle_exception(err):
    """Ensure all unhandled errors return JSON (not an HTML error page),
    so the frontend's resp.json() call never chokes on HTML."""
    logger.exception("Unhandled exception while processing request")
    status_code = getattr(err, "code", 500)
    if not isinstance(status_code, int):
        status_code = 500
    return jsonify({"error": str(err)}), status_code


@app.route("/")
def index():
    """Single-page UI: list tickets, view messages, create tickets/messages,
    update ticket status."""
    return render_template("tickets.html")


@app.route("/tickets", methods=["GET"])
def list_tickets():
    """Return all support tickets, most recently created first."""
    ensure_tickets_tables()
    rows = lakebase.run_query(
        f"SELECT ticket_id, title, status, created_by, created_at "
        f"FROM {TICKETS_TABLE} ORDER BY created_at DESC"
    )
    return jsonify(rows)


@app.route("/tickets", methods=["POST"])
def create_ticket():
    """Create a new support ticket."""
    ensure_tickets_tables()

    payload = request.get_json(silent=True) or {}
    title = (payload.get("title") or "").strip()

    if not title:
        return jsonify({"error": "title is required"}), 400

    created_by = _current_user_email()

    rows = lakebase.run_query(
        f"""
        INSERT INTO {TICKETS_TABLE} (title, status, created_by)
        VALUES (%s, 'open', %s)
        RETURNING ticket_id, title, status, created_by, created_at
        """,
        (title, created_by),
    )
    return jsonify(rows[0]), 201


@app.route("/tickets/<int:ticket_id>", methods=["GET"])
def get_ticket(ticket_id):
    """Return a single ticket and all of its messages."""
    ensure_tickets_tables()

    ticket_rows = lakebase.run_query(
        f"SELECT ticket_id, title, status, created_by, created_at "
        f"FROM {TICKETS_TABLE} WHERE ticket_id = %s",
        (ticket_id,),
    )
    if not ticket_rows:
        return jsonify({"error": f"Ticket {ticket_id} not found"}), 404

    message_rows = lakebase.run_query(
        f"SELECT message_id, ticket_id, message_text, author, created_at "
        f"FROM {TICKET_MESSAGES_TABLE} WHERE ticket_id = %s ORDER BY created_at ASC",
        (ticket_id,),
    )
    ticket = ticket_rows[0]
    ticket["messages"] = message_rows
    return jsonify(ticket)


@app.route("/tickets/<int:ticket_id>/messages", methods=["POST"])
def add_ticket_message(ticket_id):
    """Add a new message to an existing ticket."""
    ensure_tickets_tables()

    ticket_rows = lakebase.run_query(
        f"SELECT ticket_id FROM {TICKETS_TABLE} WHERE ticket_id = %s", (ticket_id,)
    )
    if not ticket_rows:
        return jsonify({"error": f"Ticket {ticket_id} not found"}), 404

    payload = request.get_json(silent=True) or {}
    message_text = (payload.get("message_text") or "").strip()

    if not message_text:
        return jsonify({"error": "message_text is required"}), 400

    author = _current_user_email()

    rows = lakebase.run_query(
        f"""
        INSERT INTO {TICKET_MESSAGES_TABLE} (ticket_id, message_text, author)
        VALUES (%s, %s, %s)
        RETURNING message_id, ticket_id, message_text, author, created_at
        """,
        (ticket_id, message_text, author),
    )
    return jsonify(rows[0]), 201


@app.route("/tickets/<int:ticket_id>/status", methods=["PATCH"])
def update_ticket_status(ticket_id):
    """Update a ticket's status (open / in_progress / resolved)."""
    ensure_tickets_tables()

    payload = request.get_json(silent=True) or {}
    status = payload.get("status")

    if status not in VALID_STATUSES:
        return jsonify(
            {"error": f"status must be one of {VALID_STATUSES}, got {status!r}"}
        ), 400

    rows = lakebase.run_query(
        f"""
        UPDATE {TICKETS_TABLE} SET status = %s WHERE ticket_id = %s
        RETURNING ticket_id, title, status, created_by, created_at
        """,
        (status, ticket_id),
    )
    if not rows:
        return jsonify({"error": f"Ticket {ticket_id} not found"}), 404

    return jsonify(rows[0])


if __name__ == '__main__':
    host = os.getenv('FLASK_RUN_HOST', '0.0.0.0')
    port = int(os.getenv('FLASK_RUN_PORT', 8000))
    app.run(debug=True, host=host, port=port)
    print(f"Flask app running on http://{host}:{port}")