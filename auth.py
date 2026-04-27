import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "main"
    out = "token.json" if label == "main" else f"token_{label}.json"
    flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
    creds = flow.run_local_server(port=0)
    with open(out, "w") as f:
        f.write(creds.to_json())
    print(f"{out} salvo (conta: {label}).")


if __name__ == "__main__":
    main()
