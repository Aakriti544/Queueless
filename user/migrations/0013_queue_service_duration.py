from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("user", "0012_customernotification")]

    operations = [
        migrations.AddField(
            model_name="queue",
            name="service_duration_minutes",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
