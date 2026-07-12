import os
import re
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import google.generativeai as genai
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, template_folder='.')
app.secret_key = os.getenv("FLASK_SECRET_KEY", "carbon-coach-fallback-key")

# Configure Gemini API
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
else:
    print("Warning: GEMINI_API_KEY is not set.")

# Database connection helper
def get_db_connection():
    db_string = os.getenv("NEON_DB_STRING")
    if not db_string:
        raise ValueError("NEON_DB_STRING environment variable is missing!")
    conn = psycopg2.connect(db_string, sslmode='require')
    return conn

# Database Initialization
def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    # Users table
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(50) UNIQUE NOT NULL,
            password VARCHAR(100) NOT NULL
        );
    ''')
    # Carbon history / goals table
    cur.execute('''
        CREATE TABLE IF NOT EXISTS carbon_data (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            footprint FLOAT DEFAULT 0.0,
            goal_reduction FLOAT DEFAULT 0.0,
            challenges_completed INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    conn.commit()
    cur.close()
    conn.close()

try:
    init_db()
except Exception as e:
    print(f"Database initialization error: {e}")

# Routes
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/auth/signup', methods=['POST'])
def signup():
    data = request.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()

    if not username or not password:
        return jsonify({'error': 'Missing credentials'}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute('INSERT INTO users (username, password) VALUES (%s, %s) RETURNING id', (username, password))
        user_id = cur.fetchone()[0]
        # Seed initial carbon data row
        cur.execute('INSERT INTO carbon_data (user_id) VALUES (%s)', (user_id,))
        conn.commit()
        session['user_id'] = user_id
        session['username'] = username
        return jsonify({'message': 'Registration successful', 'user': username})
    except psycopg2.IntegrityError:
        conn.rollback()
        return jsonify({'error': 'Username already exists'}), 400
    finally:
        cur.close()
        conn.close()

@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute('SELECT * FROM users WHERE username = %s AND password = %s', (username, password))
    user = cur.fetchone()
    cur.close()
    conn.close()

    if user:
        session['user_id'] = user['id']
        session['username'] = user['username']
        return jsonify({'message': 'Login successful', 'user': user['username']})
    return jsonify({'error': 'Invalid credentials'}), 401

@app.route('/api/auth/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'message': 'Logged out successfully'})

@app.route('/api/auth/status', methods=['GET'])
def auth_status():
    if 'user_id' in session:
        return jsonify({'logged_in': True, 'username': session['username']})
    return jsonify({'logged_in': False})

@app.route('/api/carbon/calculate', methods=['POST'])
def calculate():
    data = request.json or {}
    car = float(data.get('car', 0) or 0)
    bike = float(data.get('bike', 0) or 0)
    bus = float(data.get('bus', 0) or 0)
    train = float(data.get('train', 0) or 0)
    flight = float(data.get('flight', 0) or 0)
    electricity = float(data.get('electricity', 0) or 0)
    shopping = data.get('shopping', 'average')

    # Basic rough multiplier factors for monthly CO2 in kg
    footprint = (car * 0.2) + (bike * 0.1) + (bus * 0.05) + (train * 0.03) + (flight * 0.15) + (electricity * 0.5)
    shopping_weights = {'low': 50, 'average': 150, 'high': 300}
    footprint += shopping_weights.get(shopping, 150)
    footprint = round(footprint, 2)

    # Save to user session/DB if logged in
    if 'user_id' in session:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('UPDATE carbon_data SET footprint = %s WHERE user_id = %s', (footprint, session['user_id']))
        conn.commit()
        cur.close()
        conn.close()

    return jsonify({'footprint': footprint})

@app.route('/api/ai/coach', methods=['POST'])
def ai_coach():
    data = request.json or {}
    footprint = data.get('footprint', 'Unknown')
    context = data.get('context', '')

    prompt = (
        f"The user has a monthly carbon footprint of {footprint} kg CO2e. "
        f"Context provided by user: {context}. "
        f"Act as Carbon Coach AI. Provide exactly 4 bulleted, direct, hyper-personalized actionable recommendations "
        f"to drastically reduce this footprint. Keep it engaging and modern."
    )

    try:
        model = genai.GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(prompt)
        # Convert markdown bullets to HTML-friendly formatting or just text cleanups
        text = response.text
        tips = [re.sub(r'^[*-\d.\s]+', '', line).strip() for line in text.split('\n') if line.strip()]
        return jsonify({'tips': tips[:6]})
    except Exception as e:
        return jsonify({'tips': [
            "Replace 2 short car trips with cycling or walking.",
            "Reduce AC temperature setting by 1°C to conserve energy.",
            "Buy second-hand clothing instead of fast fashion brands.",
            "Switch your frequently used home bulbs to energy-efficient LEDs."
        ], 'error': str(e)})

@app.route('/api/ai/challenges', methods=['GET'])
def ai_challenges():
    prompt = (
        "Generate 4 completely unique, small, hyper-engaging daily eco challenges. "
        "Format your response as a simple list with one challenge per line. No introduction, no formatting asterisks."
    )
    try:
        model = genai.GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(prompt)
        challenges = [re.sub(r'^[*-\d.\s]+', '', line).strip() for line in response.text.split('\n') if line.strip()]
        return jsonify({'challenges': challenges[:4]})
    except Exception as e:
        return jsonify({'challenges': [
            "Walk or cycle instead of driving for any errand today.",
            "Skip all single-use plastics entirely for the next 24 hours.",
            "Eat completely plant-based meals throughout the day.",
            "Unplug all unused chargers and electronics before sleeping."
        ]})

@app.route('/api/dashboard/update-goal', methods=['POST'])
def update_goal():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.json or {}
    goal = float(data.get('goal', 0) or 0)

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('UPDATE carbon_data SET goal_reduction = %s WHERE user_id = %s', (goal, session['user_id']))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'message': 'Goal updated successfully'})

@app.route('/api/dashboard/complete-challenge', methods=['POST'])
def complete_challenge():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('UPDATE carbon_data SET challenges_completed = challenges_completed + 1 WHERE user_id = %s', (session['user_id'],))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'message': 'Challenge complete updated'})

@app.route('/api/dashboard/data', methods=['GET'])
def get_dashboard():
    if 'user_id' not in session:
        return jsonify({
            'footprint': 450,
            'goal_reduction': 20,
            'challenges_completed': 2,
            'guest': True
        })
    
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute('SELECT footprint, goal_reduction, challenges_completed FROM carbon_data WHERE user_id = %s', (session['user_id'],))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        row = {'footprint': 0, 'goal_reduction': 0, 'challenges_completed': 0}
    
    return jsonify(row)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
