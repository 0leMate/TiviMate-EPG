# Vortex Hybrid EPG v3.1

Zulip is the programme-data source for:

- DIRTVISION
- Paramount
- PPV
- PPV2
- Sky Sports+
- Stan Sport
- Discovery+

For those groups, the last saved Zulip schedule is retained until a newer
parseable schedule post appears.

M3U channel-name programme data remains in use for:

- Kayo+
- ESPN+
- UFC
- Sportsnet+
- TSN+
- ESPN Play

The Vortex M3U is also used as the channel/slot map for Zulip-managed groups.

The GitHub Action runs every two hours at :30.

Required GitHub Secrets:

- XTREAM_M3U_URL
- ZULIP_SITE
- ZULIP_EMAIL
- ZULIP_API_KEY
