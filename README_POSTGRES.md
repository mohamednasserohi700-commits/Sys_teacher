# الترحيل إلى PostgreSQL — دليل النشر والاستخدام

تم تحويل النظام بالكامل من SQLite إلى **PostgreSQL** كقاعدة بيانات وحيدة:

- **قاعدة رئيسية واحدة** (Schema باسم `public`) تحتوي جدولي `tenants` و `subscription_codes`
  (بيانات المستخدمين/المؤسسات والإعدادات العامة).
- **كل مؤسسة لها Schema مستقلة** داخل نفس قاعدة البيانات باسم `tenant_<slug>`
  (بدلاً من ملف SQLite منفصل)، وتحتوي جداول `users` و `system_settings` و `students` و `attendance`.
- عند فتح أي رابط `/org/<slug>/...` يقوم النظام تلقائياً بضبط `search_path` على Schema
  المؤسسة المطلوبة، فتُنفَّذ كل الاستعلامات ضمن بياناتها فقط (عزل كامل بين المؤسسات).
- لا يوجد أي مسار محلي (`data/main.db`, `data/tenants/...`) في كامل الكود.

## 1) متغير البيئة المطلوب

```
DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/DBNAME
```

هذا هو المصدر الوحيد لبيانات الاتصال بقاعدة البيانات. بدونه لن يقلع التطبيق،
وستظهر رسالة خطأ واضحة تشرح كيفية تعيينه.

متغيرات اختيارية أخرى (كما في السابق):
```
SECRET_KEY=...
DEVELOPER_USERNAME=...
DEVELOPER_PASSWORD=...
```

## 2) النشر على Railway

1. أنشئ Project جديد على Railway وارفع هذا المستودع.
2. من `+ New` أضف خدمة **PostgreSQL** (Database → PostgreSQL) داخل نفس الـ Project.
3. Railway يوفّر تلقائياً متغير `DATABASE_URL` لخدمة الـ Postgres. اربطه بخدمة التطبيق:
   في إعدادات خدمة التطبيق → Variables → أضف Reference إلى `DATABASE_URL` من خدمة Postgres
   (أو انسخ قيمته يدوياً كمتغير بيئة في خدمة التطبيق).
4. Railway سيستخدم `nixpacks.toml` / `Procfile` الموجودين تلقائياً لتشغيل:
   ```
   gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
   ```
5. عند أول إقلاع، يقوم `init_app()` تلقائياً بـ:
   - إنشاء جدولي `tenants` و `subscription_codes` في الـ public schema إن لم يكونا موجودين.
   - ترقية أي أعمدة ناقصة بأمان (`ADD COLUMN IF NOT EXISTS`).
   - إنشاء/تحديث Schema وجداول كل مؤسسة موجودة مسبقاً في جدول `tenants`.
   
   لا حاجة لأي أمر Migration يدوي إضافي لتشغيل نظام جديد بالكامل.

## 3) ترحيل بيانات قديمة من SQLite (إن وُجدت)

إن كان لديك نسخة سابقة تحتوي `data/main.db` و `data/tenants/*.db`، استخدم السكربت المرفق
**مرة واحدة فقط** بعد ضبط `DATABASE_URL` على قاعدة PostgreSQL الجديدة:

```bash
export DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/DBNAME
python scripts/migrate_sqlite_to_postgres.py --data-dir /path/to/old/data
```

- إن لم تُمرّر `--data-dir` سيبحث افتراضياً عن مجلد `data/` بجانب المشروع.
- السكربت آمن لإعادة التشغيل (Idempotent): يستخدم `ON CONFLICT ... DO NOTHING/UPDATE`،
  ولا يكرر البيانات إن أُعيد تشغيله على نفس القاعدة.
- يحافظ على نفس أرقام الـ id للطلاب/المستخدمين/الحضور، ثم يعيد ضبط تسلسل PostgreSQL
  (`SERIAL sequence`) تلقائياً بعد الإدخال.
- إن لم يجد أي بيانات SQLite قديمة، يخرج بأمان دون أي تعديل.

بعد التأكد من نجاح الترحيل وعمل النظام بشكل صحيح، يمكن حذف مجلد `data/` القديم نهائياً؛
فالتطبيق الحالي لا يقرأه أو يعتمد عليه إطلاقاً.

## 4) ملاحظات تقنية

- المكتبات المطلوبة (`requirements.txt`) لم تتغيّر فعلياً: `psycopg2-binary` و
  `sqlalchemy` كانا موجودين مسبقاً، وهما كافيان للعمل مع PostgreSQL.
- عزل المؤسسات يتم عبر `SET search_path TO "tenant_<slug>", public` في بداية كل طلب،
  ويُعاد ضبطه تلقائياً بعد كل طلب لتفادي تسرّب Schema بين الطلبات.
- حذف مؤسسة من لوحة المطوّر ينفّذ الآن `DROP SCHEMA "tenant_<slug>" CASCADE` بدلاً من
  حذف ملف `.db`.
- إنشاء مؤسسة جديدة (`/register`) ينشئ Schema جديدة تلقائياً بنفس الجداول الأساسية.
