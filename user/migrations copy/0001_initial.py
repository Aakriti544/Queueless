from django.db import migrations, models
from django.contrib.auth.hashers import make_password


def create_default_admin(apps, schema_editor):
    User = apps.get_model("user", "User")
    if not User.objects.filter(email="admin@queueless.com", role="admin").exists():
        User.objects.create(
            username="QueueLess Admin",
            email="admin@queueless.com",
            password=make_password("admin123"),
            role="admin",
        )


def remove_default_admin(apps, schema_editor):
    User = apps.get_model("user", "User")
    User.objects.filter(email="admin@queueless.com", role="admin").delete()


class Migration(migrations.Migration):
    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name="User",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("username", models.CharField(max_length=150)),
                ("email", models.EmailField(max_length=254)),
                ("password", models.CharField(max_length=128)),
                ("role", models.CharField(choices=[("customer", "Customer"), ("staff", "Staff"), ("admin", "Admin")], default="customer", max_length=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.UniqueConstraint(fields=("email", "role"), name="unique_user_email_role"),
        ),
        migrations.RunPython(create_default_admin, remove_default_admin),
    ]
