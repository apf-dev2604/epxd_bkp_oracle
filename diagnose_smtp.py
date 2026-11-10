import smtplib
import ssl

host = "smtp.gmail.com"
port = 587

with smtplib.SMTP(host, port, timeout=30) as smtp:
    print("Connected to SMTP")

    smtp.ehlo()
    print("STARTTLS advertised:", smtp.has_extn("starttls"))

    if not smtp.has_extn("starttls"):
        raise RuntimeError("SMTP server did not advertise STARTTLS")

    smtp.starttls(context=ssl.create_default_context())
    print("TLS established")

    smtp.ehlo()
    print("Authentication mechanisms:", smtp.esmtp_features.get("auth"))
    print("SMTP connection successful")
