"""
Seed demo data for local testing. Usage: python manage.py seed_demo_data """

from django.core.management.base import BaseCommand
from decimal import Decimal
from datetime import date, timedelta


class Command(BaseCommand):
    help = "Seed demo data for local recovery agent testing"

    def handle(self, *args, **options):
        from recovery_agent.models import (
            Dealer, Branch, Customer, Vehicle, RecoveryCase, PaymentRecord,
        )
        from recovery_agent.models import TTSVoice, Segment, LLMSetting

        self.stdout.write("Seeding demo data...")

        # Dealer
        dealer, _ = Dealer.objects.get_or_create(
            code="DEMO",
            defaults={"name": "Demo Dealer"},
        )
        self.stdout.write(f"  Dealer: {dealer}")

        # Branch
        branch, _ = Branch.objects.get_or_create(
            dealer=dealer, name="Main Branch",
            defaults={"address": "123 Demo Street", "city": "Mumbai"},
        )
        self.stdout.write(f"  Branch: {branch}")

        # TTS Voice
        voice, _ = TTSVoice.objects.get_or_create(
            voice_name="hi-IN-Wavenet-A",
            provider_name="google",
            defaults={"gender": "female"},
        )
        self.stdout.write(f"  Voice: {voice}")

        # Segment
        segment, _ = Segment.objects.get_or_create(
            name="service_recovery",
            defaults={"description": "Service recovery calls", "module": "service_recovery"},
        )
        self.stdout.write(f"  Segment: {segment}")

        # LLM Setting
        llm, _ = LLMSetting.objects.get_or_create(
            segment=segment,
            defaults={
                "dealer": dealer,
                "module": "service_recovery",
                "persona_name": "Aarohi",
                "agent_name": "Recovery Agent",
                "opening_line": "नमस्ते {customer_name} जी, मैं {dealer_name} से आरोही बोल रही हूँ।",
                "system_prompt": "You are Aarohi, a Hindi-speaking revenue-recovery agent.",
                "voice": voice,
            },
        )
        self.stdout.write(f"  LLMSetting: {llm}")

        # Customer
        customer, _ = Customer.objects.get_or_create(
            phone_number="+919876543210",
            defaults={
                "dealer": dealer,
                "default_branch": branch,
                "name": "Rahul Kumar",
                "preferred_language": "hi-IN",
            },
        )
        self.stdout.write(f"  Customer: {customer} (id={customer.id})")

        # Vehicle
        vehicle, _ = Vehicle.objects.get_or_create(
            customer=customer,
            registration_no="MH01AB1234",
            defaults={
                "dealer": dealer,
                "vehicle_name": "Honda City",
                "vehicle_model": "Honda City 2019",
                "next_service_due_date": date.today() + timedelta(days=7),
            },
        )
        self.stdout.write(f"  Vehicle: {vehicle}")

        # Recovery Case
        case, _ = RecoveryCase.objects.get_or_create(
            customer=customer,
            status="open",
            defaults={
                "dealer": dealer,
                "module": "service_recovery",
                "amount_due": Decimal("2500.00"),
                "outcome": "",
            },
        )
        self.stdout.write(f"  Case: {case} (id={case.id})")

        # Payment Record
        payment, _ = PaymentRecord.objects.get_or_create(
            customer=customer,
            status="pending",
            defaults={
                "dealer": dealer,
                "recovery_case": case,
                "amount": Decimal("2500.00"),
                "currency": "INR",
                "description": "Pending service payment",
                "provider": "razorpay",
            },
        )
        self.stdout.write(f"  PaymentRecord: {payment} (id={payment.id})")

        self.stdout.write(self.style.SUCCESS("\n✅ Demo data seeded!"))
        self.stdout.write(f"\nCustomer ID: {customer.id}")
        self.stdout.write(f"Phone: {customer.phone_number}")
        self.stdout.write("Try:")
        self.stdout.write(f'  curl -X POST http://localhost:8000/api/test/process-turn/ \\')
        self.stdout.write(f'    -H "Content-Type: application/json" \\')
        self.stdout.write(f'    -d \'{{"session_id": "test-1", "customer_id": {customer.id}, "customer_text": "Mera payment pending hai kya?"}}\'')