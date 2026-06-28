from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, send_file, Response
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date
import json
import urllib.parse
import os
import io
import zipfile

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'school_secret_2024_change_in_prod')

# Database: use PostgreSQL on Railway if DATABASE_URL is set, else SQLite locally
DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL if DATABASE_URL else 'sqlite:///school.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

from flask import g

@app.before_request
def load_system_settings():
    try:
        s = SystemSettings.query.first()
        if s:
            g.sys_name = s.system_name
            g.sys_subtitle = s.system_subtitle
        else:
            g.sys_name = 'نظام إدارة الطلاب'
            g.sys_subtitle = 'Student Management System'
    except:
        g.sys_name = 'نظام إدارة الطلاب'
        g.sys_subtitle = 'Student Management System'

# ─── Models ───────────────────────────────────────────────────────────────────

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(100), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(20), default='teacher')  # admin / teacher
    permissions = db.Column(db.Text, default='[]')  # JSON list of permission keys
    is_active = db.Column(db.Boolean, default=True)

    def get_permissions(self):
        try:
            return json.loads(self.permissions or '[]')
        except:
            return []

    def has_perm(self, perm):
        if self.role == 'admin':
            return True
        return perm in self.get_permissions()

class SystemSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    system_name = db.Column(db.String(200), default='نظام إدارة الطلاب')
    system_subtitle = db.Column(db.String(200), default='Student Management System')
    school_name = db.Column(db.String(200), default='')
    school_year = db.Column(db.String(50), default='')
    admin_phone = db.Column(db.String(50), default='')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    @staticmethod
    def get():
        s = SystemSettings.query.first()
        if not s:
            s = SystemSettings()
            db.session.add(s)
            db.session.commit()
        return s

class Student(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    grade = db.Column(db.String(20), nullable=False)
    semester = db.Column(db.String(20), default='الأول')
    student_phone = db.Column(db.String(20))
    parent_name = db.Column(db.String(100))
    parent_phone = db.Column(db.String(20))
    # أيام الحضور: مخزنة كـ JSON مثل ["السبت","الأحد","الاثنين"]
    attendance_days = db.Column(db.Text, default='[]')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    attendances = db.relationship('Attendance', backref='student', lazy=True, cascade='all, delete-orphan')

    def get_attendance_days(self):
        try:
            return json.loads(self.attendance_days or '[]')
        except:
            return []

    def set_attendance_days(self, days_list):
        self.attendance_days = json.dumps(days_list, ensure_ascii=False)

class Attendance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student.id'), nullable=False)
    date = db.Column(db.Date, nullable=False, default=date.today)
    status = db.Column(db.String(10), nullable=False)  # present / absent / late
    note = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# ─── Helpers ──────────────────────────────────────────────────────────────────

ARABIC_DAYS = {
    0: 'الاثنين',
    1: 'الثلاثاء',
    2: 'الأربعاء',
    3: 'الخميس',
    4: 'الجمعة',
    5: 'السبت',
    6: 'الأحد',
}

ALL_DAYS_ORDER = ['السبت', 'الأحد', 'الاثنين', 'الثلاثاء', 'الأربعاء', 'الخميس', 'الجمعة']

def get_day_name_ar(d: date) -> str:
    return ARABIC_DAYS[d.weekday()]

def student_scheduled_today(student, d: date) -> bool:
    """هل الطالب مجدول حضوره في هذا اليوم؟"""
    days = student.get_attendance_days()
    if not days:
        return True  # إذا لم تُحدد أيام، يظهر دائماً
    return get_day_name_ar(d) in days

# ─── Auth ──────────────────────────────────────────────────────────────────────

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

@app.route('/', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        user = User.query.filter_by(username=username, password=password).first()
        if user:
            session['user_id'] = user.id
            session['user_name'] = user.name
            session['user_role'] = user.role
            return redirect(url_for('dashboard'))
        error = 'اسم المستخدم أو كلمة المرور غير صحيحة'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ─── Dashboard ────────────────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    total_students = Student.query.count()
    today = date.today()
    today_records = Attendance.query.filter_by(date=today).all()
    present_today = sum(1 for r in today_records if r.status == 'present')
    absent_today = sum(1 for r in today_records if r.status == 'absent')
    late_today = sum(1 for r in today_records if r.status == 'late')
    grades = db.session.query(Student.grade, db.func.count(Student.id)).group_by(Student.grade).all()
    return render_template('dashboard.html',
        total_students=total_students,
        present_today=present_today,
        absent_today=absent_today,
        late_today=late_today,
        grades=grades,
        today=today.strftime('%Y-%m-%d')
    )

# ─── Students ─────────────────────────────────────────────────────────────────

@app.route('/students')
@login_required
def students():
    grade_filter = request.args.get('grade', '')
    search = request.args.get('search', '')
    query = Student.query
    if grade_filter:
        query = query.filter_by(grade=grade_filter)
    if search:
        query = query.filter(Student.name.ilike(f'%{search}%'))
    students_list = query.order_by(Student.name).all()
    grades = db.session.query(Student.grade).distinct().order_by(Student.grade).all()
    grades = [g[0] for g in grades]
    return render_template('students.html', students=students_list, grades=grades,
                           grade_filter=grade_filter, search=search,
                           all_days=ALL_DAYS_ORDER)

@app.route('/students/add', methods=['GET', 'POST'])
@login_required
def add_student():
    if request.method == 'POST':
        selected_days = request.form.getlist('attendance_days')
        student = Student(
            name=request.form['name'].strip(),
            grade=request.form['grade'].strip(),
            semester=request.form.get('semester', 'الأول').strip(),
            student_phone=request.form.get('student_phone', '').strip(),
            parent_name=request.form.get('parent_name', '').strip(),
            parent_phone=request.form.get('parent_phone', '').strip(),
        )
        student.set_attendance_days(selected_days)
        db.session.add(student)
        db.session.commit()
        flash('تم إضافة الطالب بنجاح', 'success')
        return redirect(url_for('students'))
    return render_template('student_form.html', student=None, action='add', all_days=ALL_DAYS_ORDER)

@app.route('/students/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_student(id):
    student = Student.query.get_or_404(id)
    if request.method == 'POST':
        selected_days = request.form.getlist('attendance_days')
        student.name = request.form['name'].strip()
        student.grade = request.form['grade'].strip()
        student.semester = request.form.get('semester', 'الأول').strip()
        student.student_phone = request.form.get('student_phone', '').strip()
        student.parent_name = request.form.get('parent_name', '').strip()
        student.parent_phone = request.form.get('parent_phone', '').strip()
        student.set_attendance_days(selected_days)
        db.session.commit()
        flash('تم تعديل بيانات الطالب', 'success')
        return redirect(url_for('students'))
    return render_template('student_form.html', student=student, action='edit', all_days=ALL_DAYS_ORDER)

@app.route('/students/delete/<int:id>', methods=['POST'])
@login_required
def delete_student(id):
    student = Student.query.get_or_404(id)
    db.session.delete(student)
    db.session.commit()
    flash('تم حذف الطالب', 'warning')
    return redirect(url_for('students'))

# ─── Attendance ───────────────────────────────────────────────────────────────

@app.route('/attendance')
@login_required
def attendance():
    selected_date = request.args.get('date', date.today().strftime('%Y-%m-%d'))
    grade_filter = request.args.get('grade', '')
    show_all = request.args.get('show_all', '0') == '1'
    try:
        att_date = datetime.strptime(selected_date, '%Y-%m-%d').date()
    except:
        att_date = date.today()

    day_name = get_day_name_ar(att_date)

    query = Student.query
    if grade_filter:
        query = query.filter_by(grade=grade_filter)
    all_students = query.order_by(Student.name).all()

    # فلترة الطلاب المجدولين في هذا اليوم
    if show_all:
        students_list = all_students
    else:
        students_list = [s for s in all_students if student_scheduled_today(s, att_date)]

    scheduled_count = len([s for s in all_students if student_scheduled_today(s, att_date)])
    total_count = len(all_students)

    existing = {a.student_id: a for a in Attendance.query.filter_by(date=att_date).all()}
    grades = db.session.query(Student.grade).distinct().order_by(Student.grade).all()
    grades = [g[0] for g in grades]

    return render_template('attendance.html',
        students=students_list,
        existing=existing,
        selected_date=selected_date,
        att_date=att_date,
        day_name=day_name,
        grades=grades,
        grade_filter=grade_filter,
        show_all=show_all,
        scheduled_count=scheduled_count,
        total_count=total_count
    )

@app.route('/attendance/save', methods=['POST'])
@login_required
def save_attendance():
    data = request.get_json()
    att_date = datetime.strptime(data['date'], '%Y-%m-%d').date()
    records = data.get('records', [])
    for rec in records:
        existing = Attendance.query.filter_by(student_id=rec['student_id'], date=att_date).first()
        if existing:
            existing.status = rec['status']
            existing.note = rec.get('note', '')
        else:
            att = Attendance(student_id=rec['student_id'], date=att_date,
                             status=rec['status'], note=rec.get('note', ''))
            db.session.add(att)
    db.session.commit()
    return jsonify({'success': True, 'message': 'تم حفظ الحضور بنجاح'})

# ─── Reports / Follow-up ──────────────────────────────────────────────────────

@app.route('/reports')
@login_required
def reports():
    grade_filter = request.args.get('grade', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    status_filter = request.args.get('status_filter', '')  # present / absent / late / ''

    query = db.session.query(Student,
        db.func.count(Attendance.id).label('total'),
        db.func.sum(db.case((Attendance.status == 'absent', 1), else_=0)).label('absences'),
        db.func.sum(db.case((Attendance.status == 'late', 1), else_=0)).label('lates'),
        db.func.sum(db.case((Attendance.status == 'present', 1), else_=0)).label('presents')
    ).outerjoin(Attendance)

    if grade_filter:
        query = query.filter(Student.grade == grade_filter)
    if date_from:
        query = query.filter(Attendance.date >= datetime.strptime(date_from, '%Y-%m-%d').date())
    if date_to:
        query = query.filter(Attendance.date <= datetime.strptime(date_to, '%Y-%m-%d').date())

    results = query.group_by(Student.id).order_by(Student.name).all()

    # فلترة بالحالة إذا طُلب ذلك
    if status_filter == 'present':
        results = [r for r in results if (r.presents or 0) > 0]
    elif status_filter == 'absent':
        results = [r for r in results if (r.absences or 0) > 0]
    elif status_filter == 'late':
        results = [r for r in results if (r.lates or 0) > 0]

    grades = db.session.query(Student.grade).distinct().order_by(Student.grade).all()
    grades = [g[0] for g in grades]

    # إحصائيات إجمالية
    total_present = sum(r.presents or 0 for r in results)
    total_absent = sum(r.absences or 0 for r in results)
    total_late = sum(r.lates or 0 for r in results)

    return render_template('reports.html', results=results, grades=grades,
                           grade_filter=grade_filter, date_from=date_from, date_to=date_to,
                           status_filter=status_filter,
                           total_present=total_present,
                           total_absent=total_absent,
                           total_late=total_late)

@app.route('/send_whatsapp/<int:student_id>')
@login_required
def send_whatsapp(student_id):
    student = Student.query.get_or_404(student_id)
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')

    query = Attendance.query.filter_by(student_id=student_id)
    if date_from:
        query = query.filter(Attendance.date >= datetime.strptime(date_from, '%Y-%m-%d').date())
    if date_to:
        query = query.filter(Attendance.date <= datetime.strptime(date_to, '%Y-%m-%d').date())

    records = query.order_by(Attendance.date).all()
    total = len(records)
    absences = sum(1 for r in records if r.status == 'absent')
    lates = sum(1 for r in records if r.status == 'late')
    presents = sum(1 for r in records if r.status == 'present')

    absent_dates = [r.date.strftime('%Y/%m/%d') for r in records if r.status == 'absent']
    late_dates = [r.date.strftime('%Y/%m/%d') for r in records if r.status == 'late']

    msg = f"📚 *تقرير متابعة الطالب*\n"
    msg += f"━━━━━━━━━━━━━━━━━━\n"
    msg += f"👤 الطالب: *{student.name}*\n"
    msg += f"🏫 الصف: {student.grade}\n"
    msg += f"━━━━━━━━━━━━━━━━━━\n"
    msg += f"✅ أيام الحضور: {presents}\n"
    msg += f"❌ أيام الغياب: {absences}\n"
    msg += f"⏰ أيام التأخر: {lates}\n"
    msg += f"📊 إجمالي الأيام: {total}\n"
    if absent_dates:
        msg += f"━━━━━━━━━━━━━━━━━━\n"
        msg += f"📅 تواريخ الغياب:\n"
        for d in absent_dates[-5:]:
            msg += f"  • {d}\n"
    if late_dates:
        msg += f"📅 تواريخ التأخر:\n"
        for d in late_dates[-5:]:
            msg += f"  • {d}\n"
    msg += f"━━━━━━━━━━━━━━━━━━\n"
    msg += f"🏫 إدارة المدرسة"

    phone = student.parent_phone.replace(' ', '').replace('-', '').replace('+', '')
    if phone.startswith('0'):
        phone = '966' + phone[1:]
    if not phone.startswith('966'):
        phone = '966' + phone

    wa_url = f"https://wa.me/{phone}?text={urllib.parse.quote(msg)}"
    return redirect(wa_url)


# ─── User Management ──────────────────────────────────────────────────────────

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('user_role') != 'admin':
            flash('هذه الصفحة للمديرين فقط', 'warning')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated

PERMISSIONS = {
    'view_students':  'عرض الطلاب',
    'add_students':   'إضافة وتعديل طلاب',
    'delete_students':'حذف طلاب',
    'attendance':     'تسجيل الحضور',
    'reports':        'عرض التقارير',
    'manage_users':   'إدارة المستخدمين',
}

@app.route('/users')
@login_required
@admin_required
def users():
    users_list = User.query.order_by(User.name).all()
    return render_template('users.html', users=users_list, permissions=PERMISSIONS)

@app.route('/users/add', methods=['GET', 'POST'])
@login_required
@admin_required
def add_user():
    if request.method == 'POST':
        username = request.form['username'].strip()
        if User.query.filter_by(username=username).first():
            flash('اسم المستخدم موجود مسبقاً', 'warning')
            return render_template('user_form.html', user=None, action='add', permissions=PERMISSIONS)
        perms = request.form.getlist('permissions')
        user = User(
            username=username,
            password=request.form['password'].strip(),
            name=request.form['name'].strip(),
            role=request.form.get('role', 'teacher'),
            permissions=json.dumps(perms, ensure_ascii=False)
        )
        db.session.add(user)
        db.session.commit()
        flash('تم إضافة المستخدم بنجاح', 'success')
        return redirect(url_for('users'))
    return render_template('user_form.html', user=None, action='add', permissions=PERMISSIONS)

@app.route('/users/edit/<int:id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_user(id):
    user = User.query.get_or_404(id)
    if request.method == 'POST':
        username = request.form['username'].strip()
        existing = User.query.filter_by(username=username).first()
        if existing and existing.id != id:
            flash('اسم المستخدم موجود مسبقاً', 'warning')
            return render_template('user_form.html', user=user, action='edit', permissions=PERMISSIONS)
        user.username = username
        user.name = request.form['name'].strip()
        user.role = request.form.get('role', 'teacher')
        perms = request.form.getlist('permissions')
        user.permissions = json.dumps(perms, ensure_ascii=False)
        new_pass = request.form.get('password', '').strip()
        if new_pass:
            user.password = new_pass
        db.session.commit()
        flash('تم تعديل المستخدم بنجاح', 'success')
        return redirect(url_for('users'))
    return render_template('user_form.html', user=user, action='edit', permissions=PERMISSIONS)

@app.route('/users/delete/<int:id>', methods=['POST'])
@login_required
@admin_required
def delete_user(id):
    if id == session.get('user_id'):
        flash('لا يمكنك حذف حسابك الحالي', 'warning')
        return redirect(url_for('users'))
    user = User.query.get_or_404(id)
    db.session.delete(user)
    db.session.commit()
    flash('تم حذف المستخدم', 'warning')
    return redirect(url_for('users'))

@app.route('/users/toggle/<int:id>', methods=['POST'])
@login_required
@admin_required
def toggle_user(id):
    if id == session.get('user_id'):
        return jsonify({'success': False, 'message': 'لا يمكنك تعطيل حسابك الحالي'})
    user = User.query.get_or_404(id)
    user.is_active = not getattr(user, 'is_active', True)
    db.session.commit()
    return jsonify({'success': True, 'is_active': user.is_active})

# ─── API for charts ───────────────────────────────────────────────────────────

@app.route('/api/student_detail/<int:student_id>')
@login_required
def api_student_detail(student_id):
    student = Student.query.get_or_404(student_id)
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')

    query = Attendance.query.filter_by(student_id=student_id)
    if date_from:
        query = query.filter(Attendance.date >= datetime.strptime(date_from, '%Y-%m-%d').date())
    if date_to:
        query = query.filter(Attendance.date <= datetime.strptime(date_to, '%Y-%m-%d').date())

    records = query.order_by(Attendance.date.desc()).all()

    STATUS_AR = {'present': 'حاضر', 'absent': 'غائب', 'late': 'متأخر'}
    days_list = []
    for r in records:
        days_list.append({
            'date': r.date.strftime('%Y/%m/%d'),
            'day_name': ARABIC_DAYS[r.date.weekday()],
            'status': r.status,
            'status_ar': STATUS_AR.get(r.status, r.status),
            'note': r.note or ''
        })

    return jsonify({
        'student': {
            'name': student.name,
            'grade': student.grade,
            'parent_name': student.parent_name or '',
            'parent_phone': student.parent_phone or '',
        },
        'records': days_list,
        'summary': {
            'total': len(records),
            'present': sum(1 for r in records if r.status == 'present'),
            'absent': sum(1 for r in records if r.status == 'absent'),
            'late': sum(1 for r in records if r.status == 'late'),
        }
    })

@app.route('/api/weekly_stats')
@login_required
def weekly_stats():
    from datetime import timedelta
    today = date.today()
    labels, presents, absents, lates = [], [], [], []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        records = Attendance.query.filter_by(date=d).all()
        labels.append(d.strftime('%a'))
        presents.append(sum(1 for r in records if r.status == 'present'))
        absents.append(sum(1 for r in records if r.status == 'absent'))
        lates.append(sum(1 for r in records if r.status == 'late'))
    return jsonify({'labels': labels, 'presents': presents, 'absents': absents, 'lates': lates})

# ─── Settings ─────────────────────────────────────────────────────────────────

@app.route('/settings', methods=['GET', 'POST'])
@login_required
@admin_required
def settings():
    s = SystemSettings.get()
    if request.method == 'POST':
        s.system_name = request.form.get('system_name', 'نظام إدارة الطلاب').strip()
        s.system_subtitle = request.form.get('system_subtitle', 'Student Management System').strip()
        s.school_name = request.form.get('school_name', '').strip()
        s.school_year = request.form.get('school_year', '').strip()
        s.admin_phone = request.form.get('admin_phone', '').strip()
        s.updated_at = datetime.utcnow()
        db.session.commit()
        flash('تم حفظ الإعدادات بنجاح', 'success')
        return redirect(url_for('settings'))
    return render_template('settings.html', settings=s,
                           students_count=Student.query.count(),
                           users_count=User.query.count(),
                           attendance_count=Attendance.query.count())

@app.route('/api/settings')
@login_required
def api_settings():
    s = SystemSettings.get()
    return jsonify({
        'system_name': s.system_name,
        'system_subtitle': s.system_subtitle,
        'school_name': s.school_name,
    })

# ─── Notifications ─────────────────────────────────────────────────────────────

@app.route('/api/notifications')
@login_required
def api_notifications():
    today = date.today()
    notifications = []
    # طلاب غائبون اليوم
    today_absent = db.session.query(Student).join(Attendance).filter(
        Attendance.date == today,
        Attendance.status == 'absent'
    ).count()
    if today_absent > 0:
        notifications.append({
            'type': 'warning',
            'title': 'غياب اليوم',
            'message': f'{today_absent} طالب غائب اليوم',
            'icon': 'absent'
        })
    # طلاب متأخرون اليوم
    today_late = db.session.query(Student).join(Attendance).filter(
        Attendance.date == today,
        Attendance.status == 'late'
    ).count()
    if today_late > 0:
        notifications.append({
            'type': 'info',
            'title': 'تأخر اليوم',
            'message': f'{today_late} طالب متأخر اليوم',
            'icon': 'late'
        })
    # إجمالي الطلاب
    total = Student.query.count()
    notifications.append({
        'type': 'success',
        'title': 'الطلاب المسجلون',
        'message': f'إجمالي {total} طالب في النظام',
        'icon': 'students'
    })
    return jsonify({'notifications': notifications, 'count': len([n for n in notifications if n['type'] in ('warning','danger')])})

# ─── Backup & Restore ──────────────────────────────────────────────────────────

@app.route('/backup/export')
@login_required
@admin_required
def backup_export():
    data = {
        'export_date': datetime.utcnow().isoformat(),
        'version': '1.2',
        'settings': {},
        'users': [],
        'students': [],
        'attendance': []
    }
    # Settings
    s = SystemSettings.get()
    data['settings'] = {
        'system_name': s.system_name,
        'system_subtitle': s.system_subtitle,
        'school_name': s.school_name,
        'school_year': s.school_year,
        'admin_phone': s.admin_phone,
    }
    # Users
    for u in User.query.all():
        data['users'].append({
            'id': u.id, 'username': u.username, 'password': u.password,
            'name': u.name, 'role': u.role, 'permissions': u.permissions,
            'is_active': u.is_active
        })
    # Students
    for st in Student.query.all():
        data['students'].append({
            'id': st.id, 'name': st.name, 'grade': st.grade,
            'semester': st.semester, 'student_phone': st.student_phone,
            'parent_name': st.parent_name, 'parent_phone': st.parent_phone,
            'attendance_days': st.attendance_days,
            'created_at': st.created_at.isoformat() if st.created_at else None
        })
    # Attendance
    for a in Attendance.query.all():
        data['attendance'].append({
            'id': a.id, 'student_id': a.student_id,
            'date': a.date.isoformat(), 'status': a.status, 'note': a.note
        })

    json_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('backup.json', json_bytes)
    buf.seek(0)
    filename = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return send_file(buf, mimetype='application/zip', as_attachment=True, download_name=filename)

@app.route('/backup/import', methods=['POST'])
@login_required
@admin_required
def backup_import():
    f = request.files.get('backup_file')
    if not f:
        flash('لم يتم اختيار ملف', 'warning')
        return redirect(url_for('settings'))
    try:
        buf = io.BytesIO(f.read())
        with zipfile.ZipFile(buf, 'r') as zf:
            # البحث عن backup.json بغض النظر عن المسار داخل الـ zip
            names = zf.namelist()
            json_name = next((n for n in names if n.endswith('backup.json')), None)
            if not json_name:
                raise ValueError('لم يتم العثور على ملف backup.json داخل الأرشيف')
            json_bytes = zf.read(json_name)
        data = json.loads(json_bytes.decode('utf-8'))

        # Restore settings
        if 'settings' in data:
            s = SystemSettings.get()
            sd = data['settings']
            s.system_name = sd.get('system_name', s.system_name)
            s.system_subtitle = sd.get('system_subtitle', s.system_subtitle)
            s.school_name = sd.get('school_name', s.school_name)
            s.school_year = sd.get('school_year', s.school_year)
            s.admin_phone = sd.get('admin_phone', s.admin_phone)

        # Clear and restore attendance
        Attendance.query.delete()
        # Clear and restore students
        Student.query.delete()
        db.session.commit()

        student_id_map = {}
        for st in data.get('students', []):
            new_st = Student(
                name=st['name'], grade=st['grade'],
                semester=st.get('semester', 'الأول'),
                student_phone=st.get('student_phone', ''),
                parent_name=st.get('parent_name', ''),
                parent_phone=st.get('parent_phone', ''),
                attendance_days=st.get('attendance_days', '[]')
            )
            db.session.add(new_st)
            db.session.flush()
            student_id_map[st['id']] = new_st.id

        for a in data.get('attendance', []):
            new_sid = student_id_map.get(a['student_id'])
            if new_sid:
                att = Attendance(
                    student_id=new_sid,
                    date=datetime.strptime(a['date'], '%Y-%m-%d').date(),
                    status=a['status'],
                    note=a.get('note', '')
                )
                db.session.add(att)

        db.session.commit()
        flash(f"تم استيراد النسخة الاحتياطية بنجاح — {len(data.get('students',[]))} طالب و {len(data.get('attendance',[]))} سجل حضور", 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'فشل الاستيراد: {str(e)}', 'warning')
    return redirect(url_for('settings'))

# ─── Init DB ──────────────────────────────────────────────────────────────────

def init_db():
    with app.app_context():
        db.create_all()
        # إضافة عمود attendance_days إذا لم يكن موجوداً
        for col, typedef in [
            ("attendance_days", "TEXT DEFAULT '[]'"),
            ("semester", "TEXT DEFAULT 'الأول'"),
            ("student_phone", "TEXT DEFAULT ''"),
        ]:
            try:
                db.session.execute(db.text(f"ALTER TABLE student ADD COLUMN {col} {typedef}"))
                db.session.commit()
            except:
                pass
        for col, typedef in [
            ("permissions", "TEXT DEFAULT '[]'"),
            ("is_active", "INTEGER DEFAULT 1"),
        ]:
            try:
                db.session.execute(db.text(f"ALTER TABLE user ADD COLUMN {col} {typedef}"))
                db.session.commit()
            except:
                pass

        # Create SystemSettings table columns if needed
        try:
            db.session.execute(db.text("ALTER TABLE system_settings ADD COLUMN school_year TEXT DEFAULT ''"))
            db.session.execute(db.text("ALTER TABLE system_settings ADD COLUMN admin_phone TEXT DEFAULT ''"))
            db.session.commit()
        except:
            pass
        # Ensure settings row exists
        SystemSettings.get()

        if not User.query.filter_by(username='admin').first():
            db.session.add(User(username='admin', password='admin123', name='المدير', role='admin'))
            db.session.add(User(username='teacher', password='teacher123', name='المعلم أحمد', role='teacher'))
        if Student.query.count() == 0:
            sample = [
                Student(name='محمد علي أحمد', grade='الصف الأول', parent_name='علي أحمد', parent_phone='0501234567', attendance_days='["السبت","الأحد","الاثنين","الثلاثاء","الأربعاء"]'),
                Student(name='سارة خالد محمد', grade='الصف الأول', parent_name='خالد محمد', parent_phone='0507654321', attendance_days='["السبت","الأحد","الاثنين","الثلاثاء","الأربعاء"]'),
                Student(name='عبدالله سالم', grade='الصف الثاني', parent_name='سالم عبدالله', parent_phone='0512345678', attendance_days='["الأحد","الثلاثاء","الخميس"]'),
                Student(name='نورة فهد العتيبي', grade='الصف الثاني', parent_name='فهد العتيبي', parent_phone='0509876543', attendance_days='["السبت","الأحد","الاثنين","الثلاثاء","الأربعاء"]'),
                Student(name='يوسف ناصر القحطاني', grade='الصف الثالث', parent_name='ناصر القحطاني', parent_phone='0551234567', attendance_days='["السبت","الاثنين","الأربعاء"]'),
            ]
            for s in sample:
                db.session.add(s)
        db.session.commit()

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_ENV', 'production') == 'development'
    app.run(debug=debug, host='0.0.0.0', port=port)
else:
    # Called by gunicorn on Railway
    init_db()
