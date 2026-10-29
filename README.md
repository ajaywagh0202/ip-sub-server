python -m uvicorn src.main:app --host 0.0.0.0 --port 5001 --reload

Windows agent flow
------------------
- Run the server, then download and run the Windows installer from the page.
- Existing clients must reinstall once to receive the updated agent and the
  per-user `itc-agent://` launch handler. Windows agents no longer start at login.
- Use Start Agent on the same Windows account/device; accept the browser's
  external-application prompt, then select Scan This PC.
- An unreachable agent may be stopped or uninstalled. The page offers Start
  and Install because the browser cannot inspect installed Windows programs.
- Success appears only after the device-scan API acknowledges the save. The
  agent exits after acknowledgement; reports remain available on the server.
- The external device-scan API must return a successful response only after
  committing the database write. HTTP 202 and explicit failure responses are
  not treated as saved. That external database service is not in this repo.
- Agent discovery uses the existing LAN/IP matching and requires the server
  to reach the client on port 7979. The Windows launch link is local only.

Validation: `python -B -m unittest discover -s tests -v`
