# Delivery Signal

A weekly delivery brief from a Jira, Azure DevOps, or other work-item export. The page says what changed, which work needs attention, and which decision is waiting. Every material claim cites a field.

The product page is [`docs/`](docs/index.html): a headline, then [Quick Start](docs/index.html#quickstart) with Apps, a one-liner, npm, or the checkout. Orbit can host that folder at `/products/delivery-signal/`. Until that section is on alextouvras.com, open the [page in this repo](https://htmlpreview.github.io/?https://github.com/AlexTouvras/delivery-signal/blob/main/docs/index.html). In your own terminal, from [`delivery-signal/`](delivery-signal/), `run` opens the configuration page. A connected Jira or Azure DevOps project is fetched. A single CSV is still `run` plus the file when there is no live connection.

Details are in [`delivery-signal/README.md`](delivery-signal/README.md).
