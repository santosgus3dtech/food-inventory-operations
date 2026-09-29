from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]
    operations = [
        migrations.RunSQL(
            sql="""
            CREATE FUNCTION foodops_protect_audit() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'Audit events are append-only';
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER audit_append_only
            BEFORE UPDATE OR DELETE ON core_auditevent
            FOR EACH ROW EXECUTE FUNCTION foodops_protect_audit();
        """,
            reverse_sql="""
            DROP TRIGGER audit_append_only ON core_auditevent;
            DROP FUNCTION foodops_protect_audit();
        """,
        )
    ]
