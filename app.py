import os, sqlite3, secrets, json
from datetime import datetime, date
from flask import Flask, request, jsonify, send_from_directory, redirect
import anthropic
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder='static')

ANTHROPIC_KEY        = os.environ.get('ANTHROPIC_API_KEY', '')
STRIPE_SECRET_KEY    = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_WEBHOOK_SECRET= os.environ.get('STRIPE_WEBHOOK_SECRET', '')
STRIPE_PRICE_ID      = os.environ.get('STRIPE_PRICE_ID', '')
APP_URL              = os.environ.get('APP_URL', 'https://dataspeak-vydp.onrender.com')
DB_PATH              = 'users.db'

FREE_LIMIT = 10   # analyses per month on free plan

# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    with get_db() as db:
        db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                email                TEXT UNIQUE NOT NULL,
                token                TEXT UNIQUE NOT NULL,
                source               TEXT DEFAULT 'web',
                plan                 TEXT DEFAULT 'free',
                usage_count          INTEGER DEFAULT 0,
                usage_reset_month    TEXT DEFAULT '',
                stripe_customer_id   TEXT DEFAULT '',
                stripe_subscription_id TEXT DEFAULT '',
                created_at           TEXT DEFAULT (datetime('now'))
            )
        ''')
        # Migrate older DBs that may lack new columns
        cols = [r[1] for r in db.execute("PRAGMA table_info(users)").fetchall()]
        migrations = {
            'plan':                   "ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'free'",
            'usage_count':            "ALTER TABLE users ADD COLUMN usage_count INTEGER DEFAULT 0",
            'usage_reset_month':      "ALTER TABLE users ADD COLUMN usage_reset_month TEXT DEFAULT ''",
            'stripe_customer_id':     "ALTER TABLE users ADD COLUMN stripe_customer_id TEXT DEFAULT ''",
            'stripe_subscription_id': "ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT DEFAULT ''",
        }
        for col, sql in migrations.items():
            if col not in cols:
                db.execute(sql)
        db.commit()

init_db()

# ── Helpers ───────────────────────────────────────────────────────────────────
def reset_usage_if_needed(db, user):
    """Reset monthly usage counter if we're in a new month."""
    this_month = date.today().strftime('%Y-%m')
    if user['usage_reset_month'] != this_month:
        db.execute('UPDATE users SET usage_count=0, usage_reset_month=? WHERE id=?',
                   (this_month, user['id']))
        db.commit()
        return 0
    return user['usage_count']

def user_can_generate(user):
    if user['plan'] == 'pro':
        return True, None
    if user['usage_count'] >= FREE_LIMIT:
        return False, f'Free limit reached ({FREE_LIMIT}/month). Upgrade to Pro for unlimited analyses.'
    return True, None

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/<path:path>')
def static_files(path):
    return send_from_directory(app.static_folder, path)

# Sign up / sign in
@app.route('/api/signup', methods=['POST'])
def signup():
    data   = request.json or {}
    email  = (data.get('email') or '').strip().lower()
    source = data.get('source', 'web')

    if not email or '@' not in email:
        return jsonify({'error': 'Please enter a valid email address.'}), 400

    with get_db() as db:
        existing = db.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
        if existing:
            usage = reset_usage_if_needed(db, existing)
            return jsonify({
                'token': existing['token'], 'email': email, 'returning': True,
                'plan': existing['plan'], 'usage': usage, 'limit': FREE_LIMIT
            })

        token = secrets.token_urlsafe(32)
        db.execute('INSERT INTO users (email, token, source) VALUES (?, ?, ?)',
                   (email, token, source))
        db.commit()

    return jsonify({'token': token, 'email': email, 'returning': False,
                    'plan': 'free', 'usage': 0, 'limit': FREE_LIMIT})

# Status check — also returns plan + usage for the frontend
@app.route('/api/status', methods=['POST'])
def status():
    data  = request.json or {}
    token = (data.get('token') or '').strip()
    if not token:
        return jsonify({'error': 'Token required.'}), 400

    with get_db() as db:
        user = db.execute('SELECT * FROM users WHERE token = ?', (token,)).fetchone()
        if not user:
            return jsonify({'error': 'Invalid token.'}), 404
        usage = reset_usage_if_needed(db, user)

    return jsonify({
        'valid': True, 'email': user['email'],
        'plan': user['plan'], 'usage': usage, 'limit': FREE_LIMIT
    })

# Admin stats
@app.route('/api/admin/stats')
def admin_stats():
    with get_db() as db:
        total  = db.execute('SELECT COUNT(*) as c FROM users').fetchone()['c']
        today  = db.execute("SELECT COUNT(*) as c FROM users WHERE date(created_at)=date('now')").fetchone()['c']
        pro    = db.execute("SELECT COUNT(*) as c FROM users WHERE plan='pro'").fetchone()['c']
        recent = db.execute('SELECT email, plan, created_at FROM users ORDER BY created_at DESC LIMIT 10').fetchall()
    return jsonify({
        'total_signups': total, 'today': today, 'pro_users': pro,
        'mrr_estimate': pro * 5,
        'recent': [{'email': r['email'], 'plan': r['plan'], 'joined': r['created_at']} for r in recent]
    })

# ── Stripe: create checkout session ──────────────────────────────────────────
@app.route('/api/create-checkout-session', methods=['POST'])
def create_checkout():
    if not STRIPE_SECRET_KEY:
        return jsonify({'error': 'Payments not configured yet.'}), 500

    import stripe
    stripe.api_key = STRIPE_SECRET_KEY

    data  = request.json or {}
    token = (data.get('token') or '').strip()

    with get_db() as db:
        user = db.execute('SELECT * FROM users WHERE token = ?', (token,)).fetchone()
    if not user:
        return jsonify({'error': 'Invalid token.'}), 401

    if user['plan'] == 'pro':
        return jsonify({'error': 'Already on Pro plan.'}), 400

    try:
        # Create or retrieve Stripe customer
        if user['stripe_customer_id']:
            customer_id = user['stripe_customer_id']
        else:
            customer = stripe.Customer.create(email=user['email'])
            customer_id = customer.id
            with get_db() as db:
                db.execute('UPDATE users SET stripe_customer_id=? WHERE token=?',
                           (customer_id, token))
                db.commit()

        session = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=['card'],
            line_items=[{'price': STRIPE_PRICE_ID, 'quantity': 1}],
            mode='subscription',
            success_url=f'{APP_URL}/?upgraded=1&token={token}',
            cancel_url=f'{APP_URL}/?cancelled=1',
            subscription_data={'trial_period_days': 7},
        )
        return jsonify({'url': session.url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Stripe: billing portal (manage/cancel) ───────────────────────────────────
@app.route('/api/billing-portal', methods=['POST'])
def billing_portal():
    if not STRIPE_SECRET_KEY:
        return jsonify({'error': 'Payments not configured yet.'}), 500

    import stripe
    stripe.api_key = STRIPE_SECRET_KEY

    data  = request.json or {}
    token = (data.get('token') or '').strip()

    with get_db() as db:
        user = db.execute('SELECT * FROM users WHERE token = ?', (token,)).fetchone()
    if not user or not user['stripe_customer_id']:
        return jsonify({'error': 'No billing account found.'}), 404

    try:
        portal = stripe.billing_portal.Session.create(
            customer=user['stripe_customer_id'],
            return_url=APP_URL,
        )
        return jsonify({'url': portal.url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Stripe webhook ────────────────────────────────────────────────────────────
@app.route('/api/stripe-webhook', methods=['POST'])
def stripe_webhook():
    if not STRIPE_SECRET_KEY:
        return 'Not configured', 200

    import stripe
    stripe.api_key = STRIPE_SECRET_KEY

    payload = request.data
    sig     = request.headers.get('Stripe-Signature', '')

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        return str(e), 400

    etype = event['type']
    data  = event['data']['object']

    if etype in ('customer.subscription.created', 'customer.subscription.updated'):
        status_val = data.get('status')
        active     = status_val in ('active', 'trialing')
        cust_id    = data.get('customer')
        sub_id     = data.get('id')
        plan       = 'pro' if active else 'free'
        with get_db() as db:
            db.execute(
                'UPDATE users SET plan=?, stripe_subscription_id=? WHERE stripe_customer_id=?',
                (plan, sub_id, cust_id)
            )
            db.commit()

    elif etype == 'customer.subscription.deleted':
        cust_id = data.get('customer')
        with get_db() as db:
            db.execute('UPDATE users SET plan=? WHERE stripe_customer_id=?', ('free', cust_id))
            db.commit()

    elif etype == 'invoice.payment_failed':
        cust_id = data.get('customer')
        with get_db() as db:
            db.execute('UPDATE users SET plan=? WHERE stripe_customer_id=?', ('free', cust_id))
            db.commit()

    return jsonify({'received': True})

# ── Generate insights ─────────────────────────────────────────────────────────
@app.route('/api/generate', methods=['POST'])
def generate():
    data  = request.json or {}
    token = (data.get('token') or '').strip()

    if not token:
        return jsonify({'error': 'Please sign in first.'}), 401

    with get_db() as db:
        user = db.execute('SELECT * FROM users WHERE token = ?', (token,)).fetchone()
        if not user:
            return jsonify({'error': 'Invalid token. Please sign in again.'}), 401

        # Reset monthly counter if needed, then check limit
        reset_usage_if_needed(db, user)
        user = db.execute('SELECT * FROM users WHERE token = ?', (token,)).fetchone()
        can, err = user_can_generate(user)
        if not can:
            return jsonify({'error': err, 'upgrade': True,
                            'usage': user['usage_count'], 'limit': FREE_LIMIT}), 403

    raw_data    = (data.get('data') or '').strip()
    context     = (data.get('context') or '').strip()
    audience    = data.get('audience', 'general')
    outputs     = data.get('outputs', ['summary', 'bullets', 'recommendations', 'email'])
    data_source = data.get('data_source', 'sql')

    if not raw_data:
        return jsonify({'error': 'No data provided.'}), 400
    if not ANTHROPIC_KEY:
        return jsonify({'error': 'Server not configured. Contact support.'}), 500

    source_context = {
        'sql':    'SQL query results (rows and columns from a database)',
        'r':      'R statistical output (data frames, summary(), lm() results, ANOVA, correlation matrices)',
        'python': 'Python/pandas output (DataFrame, describe(), groupby results)',
        'excel':  'Excel or spreadsheet data (rows, columns, possibly with pivot table output)',
        'other':  'data table or analysis output'
    }.get(data_source, 'data')

    stat_terms = ''
    if data_source == 'r':
        stat_terms = 'Use appropriate statistical language: coefficients, p-values, R-squared, confidence intervals, standard errors where relevant.'
    elif data_source == 'python':
        stat_terms = 'Reference pandas/numpy conventions where applicable.'

    output_instructions = {
        'summary':         'PLAIN ENGLISH SUMMARY: Write 2-3 clear sentences explaining what this data shows in simple language any professional can understand.',
        'bullets':         'KEY INSIGHTS: List 5-7 bullet points of the most important findings, trends, anomalies, or patterns in the data.',
        'recommendations': 'RECOMMENDATIONS: Provide 3-5 specific, actionable recommendations based on what the data reveals.',
        'email':           f'EMAIL FORMAT: Write a professional email to a {audience} sharing these findings. Include: Subject line, greeting, key findings (3-4 sentences), what it means, recommended next steps, and a sign-off.'
    }

    sections = '\n\n'.join(output_instructions[o] for o in outputs if o in output_instructions)

    prompt = f"""You are an expert data analyst. Analyze the following {source_context} and provide clear, professional insights.

DATA:
{raw_data}

{f'BUSINESS/RESEARCH QUESTION: {context}' if context else ''}
AUDIENCE: {audience}
{stat_terms}

Provide the following outputs using the EXACT section headers shown below:

{sections}

Be specific — reference actual numbers, values, and patterns from the data. Write like a senior analyst explaining to a real person."""

    try:
        client  = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        message = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=1800,
            messages=[{'role': 'user', 'content': prompt}]
        )
        # Increment usage counter
        with get_db() as db:
            db.execute('UPDATE users SET usage_count=usage_count+1 WHERE token=?', (token,))
            db.commit()
            updated = db.execute('SELECT usage_count, plan FROM users WHERE token=?', (token,)).fetchone()

        return jsonify({
            'result': message.content[0].text,
            'usage': updated['usage_count'],
            'limit': FREE_LIMIT,
            'plan': updated['plan']
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
