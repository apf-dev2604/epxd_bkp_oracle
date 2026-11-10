#!/usr/bin/env python3

import smtplib
import ssl
import getpass

print("=== GMAIL SMTP LOGIN TEST ===")

username = input("Gmail address: ").strip()
password = getpass.getpass("Gmail App Password: ").replace(" ", "")

try:
    print("\nConnecting to Gmail SMTP...")

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.ehlo()

        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()

        print("TLS connection: SUCCESS")
        print("Testing Gmail authentication...")

        smtp.login(username, password)

        print("\n=================================")
        print("GMAIL AUTHENTICATION: SUCCESS")
        print("=================================")

except smtplib.SMTPAuthenticationError as exc:
    print("\nGMAIL AUTHENTICATION: FAILED")
    print("Gmail rejected the username or App Password.")
    print("SMTP error code:", exc.smtp_code)

except smtplib.SMTPServerDisconnected:
    print("\nSMTP CONNECTION CLOSED DURING TEST")
    print("Gmail or the network disconnected the session.")
    print("This does not conclusively mean the password is wrong.")

except Exception as exc:
    print("\nTEST FAILED")
    print(type(exc).__name__, str(exc))
