import os
import smtplib
from dotenv import load_dotenv
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from utils.logger import log_event, log_dir

load_dotenv()

EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

def send_email(subject, body, attach_log=False):
    try:
        message = MIMEMultipart()
        message["From"] = EMAIL_SENDER
        message["To"] = EMAIL_RECEIVER
        message["Subject"] = subject
        message.attach(MIMEText(body, "plain"))

        if attach_log:
            log_file_path = os.path.join(log_dir, "events.log")
            if os.path.exists(log_file_path):
                with open(log_file_path, "rb") as log_file:
                    part = MIMEApplication(log_file.read(), Name="events.log")
                    part['Content-Disposition'] = 'attachment; filename="events.log"'
                    message.attach(part)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, message.as_string())

        log_event("📩 Correo enviado correctamente!")
    except Exception as e:
        log_event(f"❌ Error enviando correo: {e}")
