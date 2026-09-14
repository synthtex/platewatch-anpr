# Platewatch Home Assistant add-on

1. Copy the project directory to a Home Assistant add-on repository, keeping the `addon` folder contents together.
2. In Home Assistant, open **Settings > Add-ons > Add-on store**, open the menu, and add the repository URL (or use a local add-on repository).
3. Install **Platewatch ANPR**, enable **Start on boot** and **Show in sidebar**, then start it.
4. Open the add-on through its Ingress sidebar entry. The database is stored in the add-on `/data` directory.

Camera systems can send JSON to `http://HOME_ASSISTANT_HOST:8022/api/events` when the add-on port is exposed, or use the Ingress URL from a trusted reverse proxy. The dashboard's **Cameras** menu stores camera labels and endpoints.
