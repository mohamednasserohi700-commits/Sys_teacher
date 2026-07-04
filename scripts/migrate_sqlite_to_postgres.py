#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
سكربت ترحيل البيانات من قواعد بيانات SQLite القديمة (النسخة V2.1) إلى PostgreSQL.

يُستخدم مرة واحدة فقط بعد ترقية المشروع للعمل حصراً على PostgreSQL،
وذلك في حال وجود بيانات قديمة على هيئة:
    <data-dir>/main.db
    <data-dir>/tenants/<slug>.db

طريقة الاستخدام:
    export DATABASE_URL=postgresql://user:password@host:5432/dbname
    python scripts/migrate_sqlite_to_postgres.py --data-dir /path/to/old/data

إن لم يوجد مجلد بيانات قديم أو ملف main.db، يخرج السكربت بأمان دون أي تغيير.
هذا السكربت هو الاستثناء الوحيد الذي يقرأ مسارات محلية (ملفات .db القديمة)
لأن الهدف منه هو نقل بياناتها؛ التطبيق نفسه لا يعتمد على أي مسار محلي إطلاقاً.
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime

# تأكد من إمكانية استيراد app.py من المجلد الأب لهذا السكربت
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def parse_dt(value):
    """يحاول تحويل قيمة تاريخ نصية قادمة من SQLite إلى datetime، أو None إن تعذّر."""
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        return value
    text_val = str(value)
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(text_val[:26], fmt)
        except ValueError:
            continue
    return None


def sqlite_rows(db_path, table):
    """يرجع كل صفوف جدول من ملف SQLite كقواميس، أو [] إن لم يوجد الجدول."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(f"SELECT * FROM {table}")
        rows = [dict(r) for r in cur.fetchall()]
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return rows


def migrate_main_db(main_db_path, app_module):
    """يرحّل جدول tenants و subscription_codes من main.db إلى public schema في PostgreSQL."""
    from sqlalchemy import text

    print(f"→ قراءة قاعدة البيانات الرئيسية القديمة: {main_db_path}")
    tenants_rows = sqlite_rows(main_db_path, 'tenants')
    codes_rows = sqlite_rows(main_db_path, 'subscription_codes')
    print(f"  وُجد {len(tenants_rows)} مؤسسة و {len(codes_rows)} كود اشتراك.")

    engine = app_module.engine
    slugs = []
    with engine.begin() as conn:
        for t in tenants_rows:
            slug = t.get('slug')
            if not slug:
                continue
            slugs.append(slug)
            conn.execute(text("""
                INSERT INTO tenants
                    (slug, org_name, owner_name, owner_email, owner_phone,
                     owner_password, is_active, subscription_until, created_at)
                VALUES
                    (:slug, :org_name, :owner_name, :owner_email, :owner_phone,
                     :owner_password, :is_active, :subscription_until, :created_at)
                ON CONFLICT (slug) DO UPDATE SET
                    org_name = EXCLUDED.org_name,
                    owner_name = EXCLUDED.owner_name,
                    owner_email = EXCLUDED.owner_email,
                    owner_phone = EXCLUDED.owner_phone,
                    owner_password = EXCLUDED.owner_password,
                    is_active = EXCLUDED.is_active,
                    subscription_until = EXCLUDED.subscription_until
            """), {
                'slug': slug,
                'org_name': t.get('org_name', ''),
                'owner_name': t.get('owner_name', ''),
                'owner_email': t.get('owner_email', ''),
                'owner_phone': t.get('owner_phone'),
                'owner_password': t.get('owner_password', ''),
                'is_active': bool(t.get('is_active', 1)),
                'subscription_until': parse_dt(t.get('subscription_until')),
                'created_at': parse_dt(t.get('created_at')) or datetime.utcnow(),
            })
        for c in codes_rows:
            conn.execute(text("""
                INSERT INTO subscription_codes (code, slug, months, is_used, used_at, created_at)
                VALUES (:code, :slug, :months, :is_used, :used_at, :created_at)
                ON CONFLICT (code) DO NOTHING
            """), {
                'code': c.get('code'),
                'slug': c.get('slug'),
                'months': c.get('months', 0),
                'is_used': bool(c.get('is_used', 0)),
                'used_at': parse_dt(c.get('used_at')),
                'created_at': parse_dt(c.get('created_at')) or datetime.utcnow(),
            })
    print("  ✓ تم ترحيل جدول المؤسسات وأكواد الاشتراك إلى PostgreSQL (public schema).")
    return slugs


def migrate_tenant_db(slug, tenant_db_path, app_module):
    """يرحّل بيانات مؤسسة واحدة (users / system_settings / students / attendance)
    من ملف SQLite الخاص بها إلى Schema مستقلة في PostgreSQL، مع الحفاظ على الـ IDs."""
    from sqlalchemy import text

    print(f"  → ترحيل بيانات المؤسسة '{slug}' من {tenant_db_path}")
    app_module.create_tenant_schema_and_tables(slug)
    schema = app_module.tenant_schema_name(slug)

    users_rows = sqlite_rows(tenant_db_path, 'users')
    settings_rows = sqlite_rows(tenant_db_path, 'system_settings')
    students_rows = sqlite_rows(tenant_db_path, 'students')
    attendance_rows = sqlite_rows(tenant_db_path, 'attendance')

    with app_module.engine.begin() as conn:
        conn.execute(text(f'SET search_path TO "{schema}", public'))

        for u in users_rows:
            conn.execute(text("""
                INSERT INTO users (id, username, password, name, role, permissions, is_active)
                VALUES (:id, :username, :password, :name, :role, :permissions, :is_active)
                ON CONFLICT (id) DO NOTHING
            """), {
                'id': u.get('id'), 'username': u.get('username', ''),
                'password': u.get('password', ''), 'name': u.get('name', ''),
                'role': u.get('role', 'teacher'), 'permissions': u.get('permissions', '[]'),
                'is_active': u.get('is_active', 1),
            })

        for s in settings_rows:
            conn.execute(text("""
                INSERT INTO system_settings
                    (id, system_name, system_subtitle, school_name, school_year, admin_phone, updated_at)
                VALUES (:id, :system_name, :system_subtitle, :school_name, :school_year, :admin_phone, :updated_at)
                ON CONFLICT (id) DO NOTHING
            """), {
                'id': s.get('id'), 'system_name': s.get('system_name', ''),
                'system_subtitle': s.get('system_subtitle', ''), 'school_name': s.get('school_name', ''),
                'school_year': s.get('school_year', ''), 'admin_phone': s.get('admin_phone', ''),
                'updated_at': s.get('updated_at', ''),
            })

        for st in students_rows:
            conn.execute(text("""
                INSERT INTO students
                    (id, name, grade, semester, student_phone, parent_name, parent_phone, attendance_days, created_at)
                VALUES (:id, :name, :grade, :semester, :student_phone, :parent_name, :parent_phone, :attendance_days, :created_at)
                ON CONFLICT (id) DO NOTHING
            """), {
                'id': st.get('id'), 'name': st.get('name', ''), 'grade': st.get('grade', ''),
                'semester': st.get('semester', 'الأول'), 'student_phone': st.get('student_phone', ''),
                'parent_name': st.get('parent_name', ''), 'parent_phone': st.get('parent_phone', ''),
                'attendance_days': st.get('attendance_days', '[]'), 'created_at': st.get('created_at', ''),
            })

        for a in attendance_rows:
            conn.execute(text("""
                INSERT INTO attendance (id, student_id, date, status, note, created_at)
                VALUES (:id, :student_id, :date, :status, :note, :created_at)
                ON CONFLICT (id) DO NOTHING
            """), {
                'id': a.get('id'), 'student_id': a.get('student_id'), 'date': a.get('date', ''),
                'status': a.get('status', ''), 'note': a.get('note', ''), 'created_at': a.get('created_at', ''),
            })

        # إعادة ضبط الـ sequences بعد إدخال IDs يدوياً، حتى تستمر SERIAL بشكل صحيح
        for tbl in ('users', 'system_settings', 'students', 'attendance'):
            conn.execute(text(f"""
                SELECT setval(
                    pg_get_serial_sequence('"{schema}".{tbl}', 'id'),
                    COALESCE((SELECT MAX(id) FROM "{schema}".{tbl}), 1),
                    (SELECT MAX(id) IS NOT NULL FROM "{schema}".{tbl})
                )
            """))

    print(f"    ✓ users={len(users_rows)} settings={len(settings_rows)} "
          f"students={len(students_rows)} attendance={len(attendance_rows)}")


def main():
    parser = argparse.ArgumentParser(description='ترحيل بيانات النظام من SQLite القديم إلى PostgreSQL')
    parser.add_argument('--data-dir', default=os.environ.get('LEGACY_DATA_DIR', 'data'),
                         help='مسار مجلد البيانات القديم الذي يحتوي main.db ومجلد tenants (افتراضي: data)')
    args = parser.parse_args()

    if not os.environ.get('DATABASE_URL'):
        print("خطأ: متغير البيئة DATABASE_URL غير موجود. عيّنه أولاً ثم أعد التشغيل.")
        sys.exit(1)

    data_dir = os.path.abspath(args.data_dir)
    main_db_path = os.path.join(data_dir, 'main.db')
    tenants_dir = os.path.join(data_dir, 'tenants')

    if not os.path.exists(main_db_path) and not os.path.isdir(tenants_dir):
        print(f"لا توجد بيانات SQLite قديمة في '{data_dir}'. لا شيء لترحيله. ✓")
        return

    # استيراد app.py بعد التأكد من DATABASE_URL (يقوم تلقائياً بإنشاء جداول public schema)
    import app as app_module

    slugs_from_main = []
    if os.path.exists(main_db_path):
        slugs_from_main = migrate_main_db(main_db_path, app_module)
    else:
        print(f"تنبيه: لم يوجد {main_db_path}، سيتم الاعتماد فقط على ملفات tenants الموجودة.")

    # اجمع كل الـ slugs الممكنة: من main.db + من أسماء ملفات tenants/*.db مباشرة (احتياطاً)
    slugs = set(slugs_from_main)
    if os.path.isdir(tenants_dir):
        for fname in os.listdir(tenants_dir):
            if fname.endswith('.db'):
                slugs.add(fname[:-3])

    if not slugs:
        print("لم يتم العثور على أي مؤسسات لترحيلها.")
        return

    print(f"→ سيتم ترحيل {len(slugs)} مؤسسة: {sorted(slugs)}")
    ok, failed = 0, []
    for slug in sorted(slugs):
        tenant_db_path = os.path.join(tenants_dir, f'{slug}.db')
        if not os.path.exists(tenant_db_path):
            print(f"  تخطي '{slug}': لا يوجد ملف {tenant_db_path}")
            continue
        try:
            migrate_tenant_db(slug, tenant_db_path, app_module)
            ok += 1
        except Exception as e:
            failed.append(slug)
            print(f"  ✗ فشل ترحيل '{slug}': {e}")

    print("──────────────────────────────────────────")
    print(f"تم الانتهاء. نجح: {ok} | فشل: {len(failed)}")
    if failed:
        print(f"المؤسسات التي فشلت: {failed}")
        sys.exit(1)


if __name__ == '__main__':
    main()
