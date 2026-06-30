"""
نظام إدارة الطلاب — Multi-Tenant
كل مؤسسة لها ملف SQLite منفصل في مجلد /data/tenants/
قاعدة بيانات المؤسسات (tenants) في /data/main.db
"""

from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, send_file, g
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import create_engine, text
from sqlalchemy.orm import scoped_session, sessionmaker, declarative_base
from datetime import datetime, date, timedelta
from functools import wraps
import json, urllib.parse, os, io, zipfile, re, secrets

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'multitenant_secret_2024_change_in_prod')

# ══════════════════════════════════════════════════════════════
#  حساب مطور النظام (مخفي تماماً - غير موجود في أي قاعدة بيانات)
#  لا يظهر لأي مؤسسة ولا لأي مستخدم أو أدمن عادي
# ══════════════════════════════════════════════════════════════
DEVELOPER_USERNAME = os.environ.get('DEVELOPER_USERNAME', 'administrator')
DEVELOPER_PASSWORD = os.environ.get('DEVELOPER_PASSWORD', '3000330210')

@app.template_filter('format_date')
def format_date_filter(value, fmt='%Y/%m/%d'):
    """تحويل تاريخ نصي أو datetime إلى صيغة مناسبة للعرض."""
    if not value:
        return ''
    if hasattr(value, 'strftime'):
        return value.strftime(fmt)
    for pattern in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
        try:
            from datetime import datetime as _dt
            return _dt.strptime(str(value)[:19], pattern).strftime(fmt)
        except ValueError:
            continue
    return str(value)[:10]

# ══════════════════════════════════════════════════════════════
#  مجلدات البيانات
# ══════════════════════════════════════════════════════════════

BASE_DIR    = os.path.abspath(os.path.dirname(__file__))
DATA_DIR    = os.environ.get('DATA_DIR', os.path.join(BASE_DIR, 'data'))
TENANTS_DIR = os.path.join(DATA_DIR, 'tenants')
MAIN_DB     = os.path.join(DATA_DIR, 'main.db')

os.makedirs(TENANTS_DIR, exist_ok=True)

# ══════════════════════════════════════════════════════════════
#  قاعدة بيانات المؤسسات الرئيسية (main.db)
# ══════════════════════════════════════════════════════════════

app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{MAIN_DB}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
main_db = SQLAlchemy(app)

class Tenant(main_db.Model):
    __tablename__ = 'tenants'
    id            = main_db.Column(main_db.Integer, primary_key=True)
    slug          = main_db.Column(main_db.String(60), unique=True, nullable=False)
    org_name      = main_db.Column(main_db.String(200), nullable=False)
    owner_name    = main_db.Column(main_db.String(100), nullable=False)
    owner_email   = main_db.Column(main_db.String(150), unique=True, nullable=False)
    owner_phone   = main_db.Column(main_db.String(20), nullable=True)
    owner_password= main_db.Column(main_db.String(200), nullable=False)
    is_active     = main_db.Column(main_db.Boolean, default=True)
    subscription_until = main_db.Column(main_db.DateTime, nullable=True)
    created_at    = main_db.Column(main_db.DateTime, default=datetime.utcnow)

    def is_subscribed(self):
        return bool(self.subscription_until and self.subscription_until > datetime.utcnow())

    def db_path(self):
        return os.path.join(TENANTS_DIR, f'{self.slug}.db')


# ══════════════════════════════════════════════════════════════
#  حدود الباقة المجانية + باقات الاشتراك
# ══════════════════════════════════════════════════════════════
FREE_PLAN_MAX_USERS    = 3
FREE_PLAN_MAX_STUDENTS = 15
SUBSCRIPTION_WHATSAPP  = '01103763082'
SUBSCRIPTION_PLANS = [
    {'months': 6,  'price': 3000, 'label': '6 أشهر'},
    {'months': 12, 'price': 5500, 'label': 'سنة كاملة'},
]


class SubscriptionCode(main_db.Model):
    __tablename__ = 'subscription_codes'
    id         = main_db.Column(main_db.Integer, primary_key=True)
    code       = main_db.Column(main_db.String(40), unique=True, nullable=False)
    slug       = main_db.Column(main_db.String(60), nullable=False)   # المؤسسة المرتبط بها الكود
    months     = main_db.Column(main_db.Integer, nullable=False)
    is_used    = main_db.Column(main_db.Boolean, default=False)
    used_at    = main_db.Column(main_db.DateTime, nullable=True)
    created_at = main_db.Column(main_db.DateTime, default=datetime.utcnow)

# ══════════════════════════════════════════════════════════════
#  جلسات قواعد بيانات المؤسسات (ديناميكية)
# ══════════════════════════════════════════════════════════════

_tenant_engines = {}   # cache للـ engines

def get_tenant_engine(slug):
    if slug not in _tenant_engines:
        db_path = os.path.join(TENANTS_DIR, f'{slug}.db')
        engine  = create_engine(
            f'sqlite:///{db_path}',
            connect_args={'check_same_thread': False},
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            pool_timeout=30,
            pool_recycle=1800,
        )
        _tenant_engines[slug] = engine
    return _tenant_engines[slug]

_tenant_sessions = {}  # cache للـ scoped sessions

def get_tenant_session(slug):
    if slug not in _tenant_sessions:
        engine = get_tenant_engine(slug)
        _tenant_sessions[slug] = scoped_session(sessionmaker(bind=engine))
    return _tenant_sessions[slug]()

# ══════════════════════════════════════════════════════════════
#  إنشاء جداول المؤسسة الجديدة
# ══════════════════════════════════════════════════════════════

TENANT_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    username  TEXT UNIQUE NOT NULL,
    password  TEXT NOT NULL,
    name      TEXT NOT NULL,
    role      TEXT DEFAULT 'teacher',
    permissions TEXT DEFAULT '[]',
    is_active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS system_settings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    system_name     TEXT DEFAULT 'نظام إدارة الطلاب',
    system_subtitle TEXT DEFAULT 'Student Management System',
    school_name     TEXT DEFAULT '',
    school_year     TEXT DEFAULT '',
    admin_phone     TEXT DEFAULT '',
    updated_at      TEXT
);
CREATE TABLE IF NOT EXISTS students (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    grade           TEXT NOT NULL,
    semester        TEXT DEFAULT 'الأول',
    student_phone   TEXT DEFAULT '',
    parent_name     TEXT DEFAULT '',
    parent_phone    TEXT DEFAULT '',
    attendance_days TEXT DEFAULT '[]',
    created_at      TEXT
);
CREATE TABLE IF NOT EXISTS attendance (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    date       TEXT NOT NULL,
    status     TEXT NOT NULL,
    note       TEXT DEFAULT '',
    created_at TEXT
);
"""

def create_tenant_db(slug, org_name, owner_name, owner_email, owner_password):
    """ينشئ ملف SQLite للمؤسسة ويضع البيانات الأساسية."""
    engine = get_tenant_engine(slug)
    with engine.connect() as conn:
        for stmt in TENANT_SCHEMA.strip().split(';'):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))
        # المدير الأول
        conn.execute(text(
            "INSERT OR IGNORE INTO users (username,password,name,role,permissions,is_active) "
            "VALUES (:u,:p,:n,'admin','[]',1)"
        ), {'u': owner_email, 'p': owner_password, 'n': owner_name})
        # إعدادات المؤسسة
        conn.execute(text(
            "INSERT OR IGNORE INTO system_settings (system_name,system_subtitle,school_name,updated_at) "
            "VALUES (:n,'Student Management System',:o,:d)"
        ), {'n': org_name, 'o': org_name, 'd': datetime.utcnow().isoformat()})
        conn.commit()

# ══════════════════════════════════════════════════════════════
#  Helpers للاستعلامات على قاعدة بيانات المؤسسة
# ══════════════════════════════════════════════════════════════

def tdb():
    """إرجاع جلسة قاعدة بيانات المؤسسة الحالية."""
    return g.tenant_session

def t_fetchall(sql, params=None):
    result = tdb().execute(text(sql), params or {})
    rows   = result.fetchall()
    keys   = result.keys()
    return [dict(zip(keys, row)) for row in rows]

def t_fetchone(sql, params=None):
    result = tdb().execute(text(sql), params or {})
    row    = result.fetchone()
    return dict(zip(result.keys(), row)) if row else None

def t_execute(sql, params=None):
    tdb().execute(text(sql), params or {})
    tdb().commit()

def t_last_id():
    return tdb().execute(text("SELECT last_insert_rowid()")).scalar()

# ══════════════════════════════════════════════════════════════
#  ثوابت مشتركة
# ══════════════════════════════════════════════════════════════

ARABIC_DAYS    = {0:'الاثنين',1:'الثلاثاء',2:'الأربعاء',3:'الخميس',4:'الجمعة',5:'السبت',6:'الأحد'}
ALL_DAYS_ORDER = ['السبت','الأحد','الاثنين','الثلاثاء','الأربعاء','الخميس','الجمعة']

PERMISSIONS = {
    'view_students':  'عرض الطلاب',
    'add_students':   'إضافة وتعديل طلاب',
    'delete_students':'حذف طلاب',
    'attendance':     'تسجيل الحضور',
    'reports':        'عرض التقارير',
    'manage_users':   'إدارة المستخدمين',
}

# ══════════════════════════════════════════════════════════════
#  Decorators
# ══════════════════════════════════════════════════════════════

def load_tenant(f):
    """يتحقق من وجود المؤسسة ويفتح جلستها."""
    @wraps(f)
    def decorated(*args, **kwargs):
        slug   = kwargs.get('slug', '')
        tenant = Tenant.query.filter_by(slug=slug, is_active=True).first()
        if not tenant:
            return render_template('404.html'), 404
        g.tenant      = tenant
        g.tenant_slug = slug
        # افتح جلسة قاعدة بيانات المؤسسة
        g.tenant_session = get_tenant_session(slug)
        # إعدادات النظام للـ base.html
        s = t_fetchone("SELECT * FROM system_settings LIMIT 1")
        g.sys_name     = s['system_name']     if s else 'نظام إدارة الطلاب'
        g.sys_subtitle = s['system_subtitle'] if s else 'Student Management System'
        # حالة الاشتراك وحدود الباقة المجانية
        g.is_subscribed = tenant.is_subscribed()
        g.subscription_until = tenant.subscription_until
        if not g.is_subscribed:
            users_count    = t_fetchone("SELECT COUNT(*) AS c FROM users")['c']
            students_count = t_fetchone("SELECT COUNT(*) AS c FROM students")['c']
            g.limit_reached = (users_count >= FREE_PLAN_MAX_USERS) or (students_count >= FREE_PLAN_MAX_STUDENTS)
        else:
            g.limit_reached = False
        return f(*args, **kwargs)
    return decorated

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session or session.get('tenant_slug') != kwargs.get('slug',''):
            return redirect(url_for('tenant_login', slug=kwargs.get('slug','')))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('user_role') != 'admin':
            flash('هذه الصفحة للمديرين فقط', 'warning')
            return redirect(url_for('dashboard', slug=kwargs.get('slug','')))
        return f(*args, **kwargs)
    return decorated

def developer_required(f):
    """صفحات مطور النظام - مخفية تماماً، تُرجع 404 لأي شخص غير مسجل دخوله كمطور."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('is_developer'):
            return render_template('404.html'), 404
        return f(*args, **kwargs)
    return decorated

# ══════════════════════════════════════════════════════════════
#  الصفحات العامة (قبل الدخول)
# ══════════════════════════════════════════════════════════════


@app.teardown_appcontext
def close_tenant_session(exception=None):
    """أغلق جلسة قاعدة بيانات المؤسسة بعد كل طلب لتجنب استنزاف الـ connection pool."""
    session = g.pop('tenant_session', None)
    if session is not None:
        if exception:
            session.rollback()
        else:
            try:
                session.commit()
            except Exception:
                session.rollback()
        session.close()

@app.route('/')
def home():
    return render_template('home.html')

@app.route('/register', methods=['GET','POST'])
def register():
    error = None
    if request.method == 'POST':
        org_name  = request.form.get('org_name','').strip()
        slug_raw  = request.form.get('slug','').strip().lower()
        slug      = re.sub(r'[^a-z0-9_]', '', slug_raw)
        owner     = request.form.get('owner_name','').strip()
        owner_phone = re.sub(r'[^0-9]', '', request.form.get('owner_phone','').strip())
        email_username = re.sub(r'[^a-z0-9._-]', '', request.form.get('email_username','').strip().lower())
        email     = email_username + '@sysmakers.com' if email_username else ''
        password  = request.form.get('password','').strip()
        password2 = request.form.get('password2','').strip()

        MAX_TENANTS_PER_PHONE = 2

        if not all([org_name, slug, owner, owner_phone, email, password]):
            error = 'يرجى تعبئة جميع الحقول'
        elif len(owner_phone) < 8:
            error = 'رقم الهاتف غير صحيح'
        elif len(slug) < 3:
            error = 'رمز المؤسسة يجب أن يكون 3 أحرف على الأقل'
        elif password != password2:
            error = 'كلمتا المرور غير متطابقتين'
        elif len(password) < 6:
            error = 'كلمة المرور يجب أن تكون 6 أحرف على الأقل'
        elif slug == 'administrator':
            error = 'رمز المؤسسة غير متاح، اختر رمزاً آخر'
        elif Tenant.query.filter_by(owner_phone=owner_phone).count() >= MAX_TENANTS_PER_PHONE:
            error = 'تعذر إتمام عملية التسجيل، يرجى المحاولة لاحقاً أو التواصل مع الدعم الفني'
        elif Tenant.query.filter_by(slug=slug).first():
            error = 'رمز المؤسسة مستخدم، اختر رمزاً آخر'
        elif Tenant.query.filter_by(owner_email=email).first():
            error = 'البريد الإلكتروني مسجل مسبقاً'
        else:
            tenant = Tenant(slug=slug, org_name=org_name, owner_name=owner, owner_phone=owner_phone,
                            owner_email=email, owner_password=password)
            main_db.session.add(tenant)
            main_db.session.commit()
            create_tenant_db(slug, org_name, owner, email, password)
            flash(f'تم إنشاء مؤسستك بنجاح! | بريدك: {email} | رابط الدخول: /org/{slug}/login', 'success')
            return redirect(url_for('tenant_login', slug=slug))
    return render_template('register.html', error=error)

# ══════════════════════════════════════════════════════════════
#  روابط المؤسسة /org/<slug>/...
# ══════════════════════════════════════════════════════════════

@app.route('/org/<slug>/')
@app.route('/org/<slug>/login', methods=['GET','POST'])
@load_tenant
def tenant_login(slug):
    if 'user_id' in session and session.get('tenant_slug') == slug:
        return redirect(url_for('dashboard', slug=slug))
    error = None
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','').strip()
        user = t_fetchone(
            "SELECT * FROM users WHERE username=:u AND password=:p AND is_active=1",
            {'u': username, 'p': password}
        )
        if user:
            session.clear()
            session['user_id']    = user['id']
            session['user_name']  = user['name']
            session['user_role']  = user['role']
            session['tenant_slug']= slug
            return redirect(url_for('dashboard', slug=slug))
        error = 'اسم المستخدم أو كلمة المرور غير صحيحة'
    return render_template('login.html', error=error, slug=slug, org_name=g.tenant.org_name)

@app.route('/org/<slug>/logout')
def tenant_logout(slug):
    session.clear()
    return redirect(url_for('tenant_login', slug=slug))

# ─── Dashboard ────────────────────────────────────────────────

@app.route('/org/<slug>/dashboard')
@load_tenant
@login_required
def dashboard(slug):
    today   = date.today().isoformat()
    total   = t_fetchone("SELECT COUNT(*) c FROM students")['c']
    present = t_fetchone("SELECT COUNT(*) c FROM attendance WHERE date=:d AND status='present'",{'d':today})['c']
    absent  = t_fetchone("SELECT COUNT(*) c FROM attendance WHERE date=:d AND status='absent'", {'d':today})['c']
    late    = t_fetchone("SELECT COUNT(*) c FROM attendance WHERE date=:d AND status='late'",   {'d':today})['c']
    grades  = t_fetchall("SELECT grade, COUNT(*) cnt FROM students GROUP BY grade")
    return render_template('dashboard.html', slug=slug,
        total_students=total, present_today=present,
        absent_today=absent,  late_today=late,
        grades=[(r['grade'],r['cnt']) for r in grades],
        today=today)

# ─── Students ─────────────────────────────────────────────────

@app.route('/org/<slug>/students')
@load_tenant
@login_required
def students(slug):
    gf     = request.args.get('grade','')
    search = request.args.get('search','')
    sql    = "SELECT * FROM students WHERE 1=1"
    params = {}
    if gf:     sql += " AND grade=:g";       params['g'] = gf
    if search: sql += " AND name LIKE :s";   params['s'] = f'%{search}%'
    sql += " ORDER BY name"
    students_list = t_fetchall(sql, params)
    grades = [r['grade'] for r in t_fetchall("SELECT DISTINCT grade FROM students ORDER BY grade")]
    return render_template('students.html', slug=slug, students=students_list,
                           grades=grades, grade_filter=gf, search=search, all_days=ALL_DAYS_ORDER)

@app.route('/org/<slug>/students/add', methods=['GET','POST'])
@load_tenant
@login_required
def add_student(slug):
    if not g.is_subscribed:
        count = t_fetchone("SELECT COUNT(*) AS c FROM students")['c']
        if count >= FREE_PLAN_MAX_STUDENTS:
            flash(f'وصلت للحد الأقصى لعدد الطلاب ({FREE_PLAN_MAX_STUDENTS}) في الباقة المجانية، يرجى الاشتراك لإضافة المزيد', 'warning')
            return redirect(url_for('settings', slug=slug))
    if request.method == 'POST':
        days = json.dumps(request.form.getlist('attendance_days'), ensure_ascii=False)
        t_execute(
            "INSERT INTO students (name,grade,semester,student_phone,parent_name,parent_phone,attendance_days,created_at) "
            "VALUES (:n,:g,:se,:sp,:pn,:pp,:ad,:ca)",
            {'n':request.form['name'].strip(),'g':request.form['grade'].strip(),
             'se':request.form.get('semester','الأول').strip(),
             'sp':request.form.get('student_phone','').strip(),
             'pn':request.form.get('parent_name','').strip(),
             'pp':request.form.get('parent_phone','').strip(),
             'ad':days, 'ca':datetime.utcnow().isoformat()}
        )
        flash('تم إضافة الطالب بنجاح', 'success')
        return redirect(url_for('students', slug=slug))
    return render_template('student_form.html', slug=slug, student=None, action='add', all_days=ALL_DAYS_ORDER)

@app.route('/org/<slug>/students/edit/<int:sid>', methods=['GET','POST'])
@load_tenant
@login_required
def edit_student(slug, sid):
    student = t_fetchone("SELECT * FROM students WHERE id=:id", {'id':sid})
    if not student: return redirect(url_for('students', slug=slug))
    if request.method == 'POST':
        days = json.dumps(request.form.getlist('attendance_days'), ensure_ascii=False)
        t_execute(
            "UPDATE students SET name=:n,grade=:g,semester=:se,student_phone=:sp,"
            "parent_name=:pn,parent_phone=:pp,attendance_days=:ad WHERE id=:id",
            {'n':request.form['name'].strip(),'g':request.form['grade'].strip(),
             'se':request.form.get('semester','الأول').strip(),
             'sp':request.form.get('student_phone','').strip(),
             'pn':request.form.get('parent_name','').strip(),
             'pp':request.form.get('parent_phone','').strip(),
             'ad':days,'id':sid}
        )
        flash('تم تعديل بيانات الطالب', 'success')
        return redirect(url_for('students', slug=slug))
    student['attendance_days_list'] = json.loads(student.get('attendance_days') or '[]')
    return render_template('student_form.html', slug=slug, student=student, action='edit', all_days=ALL_DAYS_ORDER)

@app.route('/org/<slug>/students/delete/<int:sid>', methods=['POST'])
@load_tenant
@login_required
def delete_student(slug, sid):
    t_execute("DELETE FROM attendance WHERE student_id=:id", {'id':sid})
    t_execute("DELETE FROM students WHERE id=:id", {'id':sid})
    flash('تم حذف الطالب', 'warning')
    return redirect(url_for('students', slug=slug))

# ─── Attendance ───────────────────────────────────────────────

@app.route('/org/<slug>/attendance')
@load_tenant
@login_required
def attendance(slug):
    sel_date     = request.args.get('date', date.today().isoformat())
    grade_filter = request.args.get('grade','')
    show_all     = request.args.get('show_all','0') == '1'
    try:
        att_date = datetime.strptime(sel_date,'%Y-%m-%d').date()
    except:
        att_date = date.today()
    day_name = ARABIC_DAYS[att_date.weekday()]

    sql    = "SELECT * FROM students WHERE 1=1"
    params = {}
    if grade_filter: sql += " AND grade=:g"; params['g'] = grade_filter
    sql += " ORDER BY name"
    all_students = t_fetchall(sql, params)

    def scheduled(st):
        days = json.loads(st.get('attendance_days') or '[]')
        return (not days) or (day_name in days)

    for s in all_students:
        s['attendance_days_list'] = json.loads(s.get('attendance_days') or '[]')
    students_list   = all_students if show_all else [s for s in all_students if scheduled(s)]
    scheduled_count = sum(1 for s in all_students if scheduled(s))

    att_records = t_fetchall("SELECT * FROM attendance WHERE date=:d", {'d': att_date.isoformat()})
    existing    = {r['student_id']: r for r in att_records}
    grades      = [r['grade'] for r in t_fetchall("SELECT DISTINCT grade FROM students ORDER BY grade")]

    return render_template('attendance.html', slug=slug,
        students=students_list, existing=existing,
        selected_date=sel_date, att_date=att_date, day_name=day_name,
        grades=grades, grade_filter=grade_filter,
        show_all=show_all, scheduled_count=scheduled_count,
        total_count=len(all_students))

@app.route('/org/<slug>/attendance/save', methods=['POST'])
@load_tenant
@login_required
def save_attendance(slug):
    data     = request.get_json()
    att_date = data['date']
    for rec in data.get('records',[]):
        existing = t_fetchone(
            "SELECT id FROM attendance WHERE student_id=:s AND date=:d",
            {'s':rec['student_id'],'d':att_date}
        )
        if existing:
            t_execute("UPDATE attendance SET status=:st,note=:no WHERE id=:id",
                      {'st':rec['status'],'no':rec.get('note',''),'id':existing['id']})
        else:
            t_execute(
                "INSERT INTO attendance (student_id,date,status,note,created_at) VALUES (:s,:d,:st,:no,:ca)",
                {'s':rec['student_id'],'d':att_date,'st':rec['status'],
                 'no':rec.get('note',''),'ca':datetime.utcnow().isoformat()}
            )
    return jsonify({'success':True,'message':'تم حفظ الحضور بنجاح'})

# ─── Reports ──────────────────────────────────────────────────

@app.route('/org/<slug>/reports')
@load_tenant
@login_required
def reports(slug):
    gf            = request.args.get('grade','')
    date_from     = request.args.get('date_from','')
    date_to       = request.args.get('date_to','')
    status_filter = request.args.get('status_filter','')

    sql = """
        SELECT s.*,
            COUNT(a.id)                                    AS total,
            SUM(CASE WHEN a.status='absent'  THEN 1 ELSE 0 END) AS absences,
            SUM(CASE WHEN a.status='late'    THEN 1 ELSE 0 END) AS lates,
            SUM(CASE WHEN a.status='present' THEN 1 ELSE 0 END) AS presents
        FROM students s LEFT JOIN attendance a ON s.id=a.student_id
        WHERE 1=1
    """
    params = {}
    if gf:        sql += " AND s.grade=:g";    params['g']  = gf
    if date_from: sql += " AND a.date>=:df";   params['df'] = date_from
    if date_to:   sql += " AND a.date<=:dt";   params['dt'] = date_to
    sql += " GROUP BY s.id ORDER BY s.name"
    rows = t_fetchall(sql, params)

    if status_filter == 'present': rows = [r for r in rows if (r['presents'] or 0) > 0]
    elif status_filter == 'absent': rows = [r for r in rows if (r['absences'] or 0) > 0]
    elif status_filter == 'late':   rows = [r for r in rows if (r['lates']    or 0) > 0]

    grades = [r['grade'] for r in t_fetchall("SELECT DISTINCT grade FROM students ORDER BY grade")]

    class Row:
        def __init__(self,d):
            self.__dict__.update(d)
            self.student  = self
            self.absences = d.get('absences') or 0
            self.lates    = d.get('lates')    or 0
            self.presents = d.get('presents') or 0
            self.total    = d.get('total')    or 0

    results = [Row(r) for r in rows]
    return render_template('reports.html', slug=slug,
        results=results, grades=grades,
        grade_filter=gf, date_from=date_from, date_to=date_to,
        status_filter=status_filter,
        total_present=sum(r.presents for r in results),
        total_absent =sum(r.absences for r in results),
        total_late   =sum(r.lates    for r in results))

@app.route('/org/<slug>/send_whatsapp/<int:sid>')
@load_tenant
@login_required
def send_whatsapp(slug, sid):
    student  = t_fetchone("SELECT * FROM students WHERE id=:id",{'id':sid})
    if not student: return redirect(url_for('reports', slug=slug))
    date_from = request.args.get('date_from','')
    date_to   = request.args.get('date_to','')
    sql = "SELECT * FROM attendance WHERE student_id=:s"
    params = {'s':sid}
    if date_from: sql += " AND date>=:df"; params['df'] = date_from
    if date_to:   sql += " AND date<=:dt"; params['dt'] = date_to
    sql += " ORDER BY date"
    records  = t_fetchall(sql, params)
    presents = sum(1 for r in records if r['status']=='present')
    absences = sum(1 for r in records if r['status']=='absent')
    lates    = sum(1 for r in records if r['status']=='late')
    absent_dates = [r['date'] for r in records if r['status']=='absent']

    msg  = f"📚 *تقرير متابعة الطالب*\n━━━━━━━━━━━━━━━━━━\n"
    msg += f"👤 الطالب: *{student['name']}*\n🏫 الصف: {student['grade']}\n━━━━━━━━━━━━━━━━━━\n"
    msg += f"✅ أيام الحضور: {presents}\n❌ أيام الغياب: {absences}\n⏰ أيام التأخر: {lates}\n📊 إجمالي: {len(records)}\n"
    if absent_dates:
        msg += "━━━━━━━━━━━━━━━━━━\n📅 تواريخ الغياب:\n"
        for d in absent_dates[-5:]: msg += f"  • {d}\n"
    msg += "━━━━━━━━━━━━━━━━━━\n🏫 إدارة المدرسة"

    phone = (student['parent_phone'] or '').replace(' ','').replace('-','').replace('+','')
    if phone.startswith('0'): phone = '966' + phone[1:]
    if not phone.startswith('966'): phone = '966' + phone
    return redirect(f"https://wa.me/{phone}?text={urllib.parse.quote(msg)}")

# ─── Users ────────────────────────────────────────────────────

@app.route('/org/<slug>/users')
@load_tenant
@login_required
@admin_required
def users(slug):
    users_list = t_fetchall("SELECT * FROM users ORDER BY name")
    for u in users_list:
        u['permissions_list'] = json.loads(u.get('permissions') or '[]')
    return render_template('users.html', slug=slug, users=users_list, permissions=PERMISSIONS)

@app.route('/org/<slug>/users/add', methods=['GET','POST'])
@load_tenant
@login_required
@admin_required
def add_user(slug):
    if not g.is_subscribed:
        count = t_fetchone("SELECT COUNT(*) AS c FROM users")['c']
        if count >= FREE_PLAN_MAX_USERS:
            flash(f'وصلت للحد الأقصى لعدد المستخدمين ({FREE_PLAN_MAX_USERS}) في الباقة المجانية، يرجى الاشتراك لإضافة المزيد', 'warning')
            return redirect(url_for('settings', slug=slug))
    if request.method == 'POST':
        username = request.form['username'].strip()
        if t_fetchone("SELECT id FROM users WHERE username=:u",{'u':username}):
            flash('اسم المستخدم موجود مسبقاً','warning')
            return render_template('user_form.html', slug=slug, user=None, action='add', permissions=PERMISSIONS)
        perms = json.dumps(request.form.getlist('permissions'), ensure_ascii=False)
        t_execute(
            "INSERT INTO users (username,password,name,role,permissions,is_active) VALUES (:u,:p,:n,:r,:perms,1)",
            {'u':username,'p':request.form['password'].strip(),
             'n':request.form['name'].strip(),'r':request.form.get('role','teacher'),'perms':perms}
        )
        flash('تم إضافة المستخدم بنجاح','success')
        return redirect(url_for('users', slug=slug))
    return render_template('user_form.html', slug=slug, user=None, action='add', permissions=PERMISSIONS)

@app.route('/org/<slug>/users/edit/<int:uid>', methods=['GET','POST'])
@load_tenant
@login_required
@admin_required
def edit_user(slug, uid):
    user = t_fetchone("SELECT * FROM users WHERE id=:id",{'id':uid})
    if not user: return redirect(url_for('users', slug=slug))
    if request.method == 'POST':
        username = request.form['username'].strip()
        dup = t_fetchone("SELECT id FROM users WHERE username=:u AND id!=:id",{'u':username,'id':uid})
        if dup:
            flash('اسم المستخدم موجود مسبقاً','warning')
            return render_template('user_form.html', slug=slug, user=user, action='edit', permissions=PERMISSIONS)
        perms    = json.dumps(request.form.getlist('permissions'), ensure_ascii=False)
        new_pass = request.form.get('password','').strip()
        if new_pass:
            t_execute("UPDATE users SET username=:u,name=:n,role=:r,permissions=:perms,password=:p WHERE id=:id",
                {'u':username,'n':request.form['name'].strip(),'r':request.form.get('role','teacher'),
                 'perms':perms,'p':new_pass,'id':uid})
        else:
            t_execute("UPDATE users SET username=:u,name=:n,role=:r,permissions=:perms WHERE id=:id",
                {'u':username,'n':request.form['name'].strip(),'r':request.form.get('role','teacher'),
                 'perms':perms,'id':uid})
        flash('تم تعديل المستخدم بنجاح','success')
        return redirect(url_for('users', slug=slug))
    user['permissions_list'] = json.loads(user.get('permissions') or '[]')
    return render_template('user_form.html', slug=slug, user=user, action='edit', permissions=PERMISSIONS)

@app.route('/org/<slug>/users/delete/<int:uid>', methods=['POST'])
@load_tenant
@login_required
@admin_required
def delete_user(slug, uid):
    if uid == session.get('user_id'):
        flash('لا يمكنك حذف حسابك الحالي','warning')
        return redirect(url_for('users', slug=slug))
    t_execute("DELETE FROM users WHERE id=:id",{'id':uid})
    flash('تم حذف المستخدم','warning')
    return redirect(url_for('users', slug=slug))

@app.route('/org/<slug>/users/toggle/<int:uid>', methods=['POST'])
@load_tenant
@login_required
@admin_required
def toggle_user(slug, uid):
    if uid == session.get('user_id'):
        return jsonify({'success':False,'message':'لا يمكنك تعطيل حسابك الحالي'})
    user = t_fetchone("SELECT * FROM users WHERE id=:id",{'id':uid})
    new_val = 0 if user['is_active'] else 1
    t_execute("UPDATE users SET is_active=:v WHERE id=:id",{'v':new_val,'id':uid})
    return jsonify({'success':True,'is_active':bool(new_val)})

# ─── Settings ─────────────────────────────────────────────────

@app.route('/org/<slug>/settings', methods=['GET','POST'])
@load_tenant
@login_required
@admin_required
def settings(slug):
    s = t_fetchone("SELECT * FROM system_settings LIMIT 1")
    if request.method == 'POST':
        if request.form.get('form_type') == 'activate_subscription':
            code_str = request.form.get('activation_code','').strip().upper()
            sub_code = SubscriptionCode.query.filter_by(code=code_str, slug=slug, is_used=False).first()
            if not sub_code:
                flash('الكود غير صحيح أو غير صالح لهذه المؤسسة', 'danger')
            else:
                base = g.tenant.subscription_until if g.tenant.is_subscribed() else datetime.utcnow()
                g.tenant.subscription_until = base + timedelta(days=30 * sub_code.months)
                sub_code.is_used = True
                sub_code.used_at = datetime.utcnow()
                main_db.session.commit()
                flash(f'تم تفعيل الاشتراك بنجاح لمدة {sub_code.months} شهر', 'success')
            return redirect(url_for('settings', slug=slug))
        t_execute(
            "UPDATE system_settings SET system_name=:n,system_subtitle=:s,school_name=:sn,school_year=:sy,admin_phone=:ap,updated_at=:u",
            {'n':request.form.get('system_name','').strip(),
             's':request.form.get('system_subtitle','').strip(),
             'sn':request.form.get('school_name','').strip(),
             'sy':request.form.get('school_year','').strip(),
             'ap':request.form.get('admin_phone','').strip(),
             'u':datetime.utcnow().isoformat()}
        )
        flash('تم حفظ الإعدادات بنجاح','success')
        return redirect(url_for('settings', slug=slug))
    sc = t_fetchone("SELECT COUNT(*) c FROM students")['c']
    uc = t_fetchone("SELECT COUNT(*) c FROM users")['c']
    ac = t_fetchone("SELECT COUNT(*) c FROM attendance")['c']
    return render_template('settings.html', slug=slug, settings=s,
                           students_count=sc, users_count=uc, attendance_count=ac,
                           is_subscribed=g.is_subscribed, subscription_until=g.tenant.subscription_until,
                           free_max_users=FREE_PLAN_MAX_USERS, free_max_students=FREE_PLAN_MAX_STUDENTS,
                           subscription_plans=SUBSCRIPTION_PLANS, whatsapp_number=SUBSCRIPTION_WHATSAPP)

# ─── API ──────────────────────────────────────────────────────

@app.route('/org/<slug>/api/settings')
@load_tenant
@login_required
def api_settings(slug):
    s = t_fetchone("SELECT * FROM system_settings LIMIT 1")
    return jsonify({
        'system_name':     s['system_name']     if s else '',
        'system_subtitle': s['system_subtitle'] if s else '',
        'school_name':     s['school_name']     if s else ''
    })

@app.route('/org/<slug>/api/notifications')
@load_tenant
@login_required
def api_notifications(slug):
    today  = date.today().isoformat()
    absent = t_fetchone("SELECT COUNT(*) c FROM attendance WHERE date=:d AND status='absent'",{'d':today})['c']
    late   = t_fetchone("SELECT COUNT(*) c FROM attendance WHERE date=:d AND status='late'",  {'d':today})['c']
    total  = t_fetchone("SELECT COUNT(*) c FROM students")['c']
    notes  = []
    if absent > 0: notes.append({'type':'warning','title':'غياب اليوم',    'message':f'{absent} طالب غائب اليوم',  'icon':'absent'})
    if late   > 0: notes.append({'type':'info',   'title':'تأخر اليوم',    'message':f'{late} طالب متأخر اليوم',   'icon':'late'})
    notes.append(              {'type':'success', 'title':'الطلاب المسجلون','message':f'إجمالي {total} طالب في النظام','icon':'students'})
    return jsonify({'notifications':notes,'count':sum(1 for n in notes if n['type'] in ('warning','danger'))})

@app.route('/org/<slug>/api/student_detail/<int:sid>')
@load_tenant
@login_required
def api_student_detail(slug, sid):
    student = t_fetchone("SELECT * FROM students WHERE id=:id",{'id':sid})
    if not student: return jsonify({'error':'not found'}),404
    df  = request.args.get('date_from','')
    dt  = request.args.get('date_to','')
    sql = "SELECT * FROM attendance WHERE student_id=:s"
    params = {'s':sid}
    if df: sql += " AND date>=:df"; params['df'] = df
    if dt: sql += " AND date<=:dt"; params['dt'] = dt
    sql += " ORDER BY date DESC"
    records   = t_fetchall(sql, params)
    STATUS_AR = {'present':'حاضر','absent':'غائب','late':'متأخر'}
    days_list = [{'date':r['date'],
                  'day_name':ARABIC_DAYS[datetime.strptime(r['date'],'%Y-%m-%d').weekday()],
                  'status':r['status'],'status_ar':STATUS_AR.get(r['status'],r['status']),
                  'note':r['note'] or ''} for r in records]
    return jsonify({'student':{'name':student['name'],'grade':student['grade'],
                               'parent_name':student.get('parent_name',''),
                               'parent_phone':student.get('parent_phone','')},
                   'records':days_list,
                   'summary':{'total':len(records),
                              'present':sum(1 for r in records if r['status']=='present'),
                              'absent': sum(1 for r in records if r['status']=='absent'),
                              'late':   sum(1 for r in records if r['status']=='late')}})

@app.route('/org/<slug>/api/weekly_stats')
@load_tenant
@login_required
def weekly_stats(slug):
    from datetime import timedelta
    today = date.today()
    labels, presents, absents, lates = [], [], [], []
    for i in range(6,-1,-1):
        d = (today - timedelta(days=i)).isoformat()
        labels.append(d[-5:])
        presents.append(t_fetchone("SELECT COUNT(*) c FROM attendance WHERE date=:d AND status='present'",{'d':d})['c'])
        absents.append( t_fetchone("SELECT COUNT(*) c FROM attendance WHERE date=:d AND status='absent'", {'d':d})['c'])
        lates.append(   t_fetchone("SELECT COUNT(*) c FROM attendance WHERE date=:d AND status='late'",   {'d':d})['c'])
    return jsonify({'labels':labels,'presents':presents,'absents':absents,'lates':lates})

# ─── Backup / Restore ─────────────────────────────────────────

@app.route('/org/<slug>/backup/export')
@load_tenant
@login_required
@admin_required
def backup_export(slug):
    s        = t_fetchone("SELECT * FROM system_settings LIMIT 1") or {}
    users_d  = t_fetchall("SELECT * FROM users")
    students_d = t_fetchall("SELECT * FROM students")
    att_d    = t_fetchall("SELECT * FROM attendance")
    data = {'export_date':datetime.utcnow().isoformat(),'version':'3.0',
            'org_name':g.tenant.org_name,
            'settings':s,'users':users_d,'students':students_d,'attendance':att_d}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('backup.json', json.dumps(data, ensure_ascii=False, indent=2))
    buf.seek(0)
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name=f"backup_{slug}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip")

@app.route('/org/<slug>/backup/import', methods=['POST'])
@load_tenant
@login_required
@admin_required
def backup_import(slug):
    f = request.files.get('backup_file')
    if not f:
        flash('لم يتم اختيار ملف','warning')
        return redirect(url_for('settings', slug=slug))
    try:
        buf = io.BytesIO(f.read())
        with zipfile.ZipFile(buf,'r') as zf:
            name = next((n for n in zf.namelist() if n.endswith('backup.json')),None)
            if not name: raise ValueError('ملف غير صالح')
            data = json.loads(zf.read(name).decode('utf-8'))
        if 'settings' in data and data['settings']:
            sd = data['settings']
            t_execute("UPDATE system_settings SET system_name=:n,system_subtitle=:s,school_name=:sn,school_year=:sy,admin_phone=:ap",
                {'n':sd.get('system_name',''),'s':sd.get('system_subtitle',''),
                 'sn':sd.get('school_name',''),'sy':sd.get('school_year',''),'ap':sd.get('admin_phone','')})
        t_execute("DELETE FROM attendance")
        t_execute("DELETE FROM students")
        sid_map = {}
        for st in data.get('students',[]):
            t_execute(
                "INSERT INTO students (name,grade,semester,student_phone,parent_name,parent_phone,attendance_days,created_at) "
                "VALUES (:n,:g,:se,:sp,:pn,:pp,:ad,:ca)",
                {'n':st['name'],'g':st['grade'],'se':st.get('semester','الأول'),
                 'sp':st.get('student_phone',''),'pn':st.get('parent_name',''),
                 'pp':st.get('parent_phone',''),'ad':st.get('attendance_days','[]'),
                 'ca':st.get('created_at','')}
            )
            sid_map[st['id']] = t_last_id()
        for a in data.get('attendance',[]):
            new_sid = sid_map.get(a['student_id'])
            if new_sid:
                t_execute("INSERT INTO attendance (student_id,date,status,note) VALUES (:s,:d,:st,:no)",
                    {'s':new_sid,'d':a['date'],'st':a['status'],'no':a.get('note','')})
        flash(f"تم الاستيراد — {len(data.get('students',[]))} طالب",'success')
    except Exception as e:
        flash(f'فشل الاستيراد: {str(e)}','warning')
    return redirect(url_for('settings', slug=slug))

# ══════════════════════════════════════════════════════════════
#  Error handlers
# ══════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════
#  مطور النظام — صفحات مخفية (غير مرتبطة بأي رابط في الواجهة)
#  الدخول من: /system/developer-login
# ══════════════════════════════════════════════════════════════

@app.route('/system/developer-login', methods=['GET','POST'])
def developer_login():
    error = None
    if request.method == 'POST':
        u = request.form.get('username','').strip()
        p = request.form.get('password','').strip()
        if u == DEVELOPER_USERNAME and p == DEVELOPER_PASSWORD:
            session.clear()
            session['is_developer'] = True
            session['dev_name']     = 'محمد ناصر'
            return redirect(url_for('developer_dashboard'))
        error = 'بيانات الدخول غير صحيحة'
    return render_template('developer_login.html', error=error)

@app.route('/system/developer-logout')
def developer_logout():
    session.clear()
    return redirect(url_for('developer_login'))

@app.route('/system/developer-dashboard')
@developer_required
def developer_dashboard():
    tenants = Tenant.query.order_by(Tenant.created_at.desc()).all()
    return render_template('developer_dashboard.html', tenants=tenants)

@app.route('/system/developer-toggle/<slug>', methods=['POST'])
@developer_required
def developer_toggle_tenant(slug):
    tenant = Tenant.query.filter_by(slug=slug).first()
    if tenant:
        tenant.is_active = not tenant.is_active
        main_db.session.commit()
        return jsonify({'success': True, 'is_active': tenant.is_active})
    return jsonify({'success': False}), 404

@app.route('/system/developer-delete/<slug>', methods=['POST'])
@developer_required
def developer_delete_tenant(slug):
    tenant = Tenant.query.filter_by(slug=slug).first()
    if not tenant:
        return jsonify({'success': False}), 404
    # حذف أكواد الاشتراك المرتبطة بالمؤسسة
    SubscriptionCode.query.filter_by(slug=slug).delete()
    # حذف ملف قاعدة بيانات المؤسسة لو موجود
    db_path = tenant.db_path()
    _tenant_engines.pop(slug, None)
    _tenant_sessions.pop(slug, None)
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except Exception:
            pass
    main_db.session.delete(tenant)
    main_db.session.commit()
    return jsonify({'success': True})

@app.route('/system/developer-subscriptions', methods=['GET','POST'])
@developer_required
def developer_subscriptions():
    if request.method == 'POST':
        slug   = request.form.get('slug','').strip()
        months = int(request.form.get('months', 0) or 0)
        tenant = Tenant.query.filter_by(slug=slug).first()
        if not tenant or months <= 0:
            flash('بيانات غير صحيحة', 'danger')
        else:
            code = secrets.token_hex(4).upper()
            sc = SubscriptionCode(code=code, slug=slug, months=months)
            main_db.session.add(sc)
            main_db.session.commit()
            flash(f'تم إنشاء الكود: {code} لمؤسسة {tenant.org_name} لمدة {months} شهر', 'success')
        return redirect(url_for('developer_subscriptions'))
    tenants = Tenant.query.order_by(Tenant.org_name).all()
    codes   = SubscriptionCode.query.order_by(SubscriptionCode.created_at.desc()).all()
    tenant_map = {t.slug: t for t in tenants}
    return render_template('developer_subscriptions.html', tenants=tenants, codes=codes, tenant_map=tenant_map)


@app.route('/system/developer-enter/<slug>')
@developer_required
def developer_enter(slug):
    """يفتح أي مؤسسة كأنه أدمن فيها، دون أن يظهر كمستخدم في قاعدة بيانات المؤسسة."""
    tenant = Tenant.query.filter_by(slug=slug).first()
    if not tenant:
        return render_template('404.html'), 404
    session['user_id']     = 0
    session['user_name']   = 'محمد ناصر (مطور النظام)'
    session['user_role']   = 'admin'
    session['tenant_slug'] = slug
    session['is_developer']= True
    return redirect(url_for('dashboard', slug=slug))



# ══════════════════════════════════════════════════════════════
#  Init
# ══════════════════════════════════════════════════════════════

def init_app():
    with app.app_context():
        main_db.create_all()
        # ترقية قاعدة البيانات الرئيسية: إضافة عمود owner_phone لو مش موجود
        try:
            with main_db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE tenants ADD COLUMN owner_phone VARCHAR(20)"))
                conn.commit()
        except Exception:
            pass  # العمود موجود بالفعل
        try:
            with main_db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE tenants ADD COLUMN subscription_until DATETIME"))
                conn.commit()
        except Exception:
            pass  # العمود موجود بالفعل
        # ترقية قواعد بيانات المؤسسات القديمة: إضافة أي جداول ناقصة
        # (مثل system_settings) من غير ما نلمس بياناتها الحالية
        try:
            tenants = Tenant.query.all()
        except Exception:
            tenants = []
        for t in tenants:
            try:
                engine = get_tenant_engine(t.slug)
                with engine.connect() as conn:
                    for stmt in TENANT_SCHEMA.strip().split(';'):
                        stmt = stmt.strip()
                        if stmt:
                            conn.execute(text(stmt))
                    conn.commit()
            except Exception as e:
                print(f'[migration] فشل تحديث قاعدة بيانات {t.slug}: {e}')

if __name__ == '__main__':
    init_app()
    port  = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_ENV','production') == 'development'
    app.run(debug=debug, host='0.0.0.0', port=port)
else:
    init_app()
