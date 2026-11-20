from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [("user", "0016_queuewaitnotice")]

    operations = [
        migrations.CreateModel(
            name="PasswordResetOTP",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("email", models.EmailField(db_index=True, max_length=254)),
                ("flow_id", models.CharField(max_length=64, unique=True)),
                ("otp_hash", models.CharField(blank=True, max_length=128)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("expires_at", models.DateTimeField()),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("resend_count", models.PositiveSmallIntegerField(default=0)),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
            ],
        ),
    ]
