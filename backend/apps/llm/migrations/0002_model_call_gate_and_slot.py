from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("llm", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="modelcall", name="slot_expires_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.CreateModel(
            name="ModelCallGate",
            fields=[("id", models.PositiveSmallIntegerField(
                default=1, editable=False, primary_key=True, serialize=False,
            ))],
            options={"db_table": "llm_model_call_gate", "constraints": [
                models.CheckConstraint(condition=models.Q(id=1), name="model_call_gate_singleton"),
            ]},
        ),
    ]
