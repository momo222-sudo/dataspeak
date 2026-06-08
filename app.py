import os, sqlite3, secrets
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
import anthropic
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder='static')

ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
DB_PATH       = 'users.db'

def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    with get_db() as db:
        db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                email      TEXT UNIQUE NOT NULL,
                token      TEXT UNIQUE NOT NULL,
                source     TEXT DEFAULT 'web',
                created_at TEXT DEFAULT (datetime('now'))
            )
        ''')
        db.commit()

init_db()

@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/<path:path>')
def static_files(path):
    return send_from_directory(app.static_folder, path)

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
            return jsonify({'token': existing['token'], 'email': email, 'returning': True})
        token = secrets.token_urlsafe(32)
        db.execute('INSERT INTO users (email, token, source) VALUES (?, ?, ?)', (email, token, source))
        db.commit()
    return jsonify({'token': token, 'email': email, 'returning': False})

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
    return jsonify({'valid': True, 'email': user['email']})

@app.route('/api/admin/stats')
def admin_stats():
    with get_db() as db:
        total  = db.execute('SELECT COUNT(*) as c FROM users').fetchone()['c']
        today  = db.execute("SELECT COUNT(*) as c FROM users WHERE date(created_at) = date('now')").fetchone()['c']
        recent = db.execute('SELECT email, created_at FROM users ORDER BY created_at DESC LIMIT 10').fetchall()
    return jsonify({'total_signups': total, 'today': today, 'recent': [{'email': r['email'], 'joined': r['created_at']} for r in recent]})

@app.route('/api/generate', methods=['POST'])
def generate():
    data   = request.json or {}
    token  = (data.get('token') or '').strip()
    if not token:
        return jsonify({'error': 'Please sign in first.'}), 401
    with get_db() as db:
        user = db.execute('SELECT * FROM users WHERE token = ?', (token,)).fetchone()
    if not user:
        return jsonify({'error': 'Invalid token. Please sign in again.'}), 401
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
        'r':      'R statistical output (could include data frames, summary(), lm() regression results, ANOVA tables, correlation matrices, or tidyverse tibbles)',
        'python': 'Python/pandas output (DataFrame, describe(), groupby results, or similar)',
        'excel':  'Excel or spreadsheet data (rows and columns, possibly with formulas or pivot table output)',
        'other':  'data table or analysis output'
    }.get(data_source, 'data')
    stat_terms = ''
    if data_source == 'r':
        stat_terms = 'Use appropriate statistical language: coefficients, p-values, R-squared, confidence intervals, standard errors where relevant.'
    elif data_source == 'python':
        stat_terms = 'Reference pandas/numpy conventions where applicable.'
    output_instructions = {
        'summary':        'PLAIN ENGLISH SUMMARY: Write 2-3 clear sentences explaining what this data shows in simple language any professional can understand.',
        'bullets':        'KEY INSIGHTS: List 5-7 bullet points of the most important findings, trends, anomalies, or patterns in the data.',
        'recommendations':'RECOMMENDATIONS: Provide 3-5 specific, actionable recommendations based on what the data reveals.',
        'email':          f'EMAIL FORMAT: Write a professional email to a {audience} sharing these findings. Include: Subject line, greeting, key findings (3-4 sentences), what it means, recommended next steps, and a sign-off.'
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

Be specific - reference actual numbers, values, and patterns from the data. Write like a senior analyst explaining to a real person."""
    try:
        client  = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        message = client.messages.create(model='claude-3-5-haiku-20241022', max_tokens=1800, messages=[{'role': 'user', 'content': prompt}])
        return jsonify({'result': message.content[0].text})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
