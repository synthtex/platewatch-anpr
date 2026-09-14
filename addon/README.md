# Platewatch Home Assistant add-on

1. In Home Assistant, open **Settings > Add-ons > Add-on store**.
2. Open the top-right menu, choose **Repositories**, and add `https://github.com/synthtex/platewatch-anpr`.
3. Refresh the store, select **Platewatch ANPR**, and install it.
4. Start the add-on. On its information page, enable **Show in sidebar**.
5. Select **Platewatch** in the left sidebar. Home Assistant opens the dashboard through Ingress, with no URL or port required for the browser.

Camera systems can send JSON to `http://HOME_ASSISTANT_HOST:8022/api/events` when the add-on port is exposed, or use the Ingress URL from a trusted reverse proxy. The dashboard's **Cameras** menu stores camera labels and endpoints.
