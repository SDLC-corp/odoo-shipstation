{
    "name": "ShipStation Connector",
    "version": "18.0.1.0.0",
    "summary": "ShipStation connector with instances, sync data, reports, and dashboard",
    "description": """
        SDLC ShipStation Connector
        ==========================
        Connect Odoo with ShipStation for a streamlined, bidirectional
        shipping workflow. Sync orders, customers, products, categories,
        and inventory between Odoo and ShipStation, process shipments
        and labels, and monitor operations from a unified dashboard.

        Features:
        ---------
        * Multi-instance ShipStation configuration
        * Order, customer, product, category and inventory sync
        * Shipment processing with label and tracking handling
        * Field mapping for clean data alignment
        * Sync logs, reports, and operational dashboard
        * Scheduled cron-based sync
    """,
    "category": "Inventory",
    "author": "SDLC Corp",
    "website": "https://sdlccorp.com/",
    "maintainer": "SDLC Corp",
    "support": "sales@sdlccorp.com",
    "license": "LGPL-3",
    "depends": [
        "base",
        "web",
        "sale_management",
        "stock",
        "delivery",
        "contacts",
    ],
    "data": [
        "security/shipstation_security.xml",
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "views/shipstation_instance_view.xml",
        "views/shipstation_field_mapping_views.xml",
        "views/shipstation_order_sync_view.xml",
        "views/shipstation_customer_sync_view.xml",
        "views/shipstation_shipment_sync_view.xml",
        "views/shipstation_product_sync_view.xml",
        "views/shipstation_inventory_view.xml",
        "views/shipstation_category_sync_view.xml",
        "views/shipstation_attribute_sync_view.xml",
        "views/shipstation_report_view.xml",
        "views/shipstation_sync_log_view.xml",
        "views/shipstation_res_config_settings_view.xml",
        "views/shipstation_dashboard_action.xml",
        "views/shipstation_actions.xml",
        "views/menu.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "shipstation_connector/static/src/css/shipstation_dashboard.css",
            "shipstation_connector/static/src/scss/chatbot.scss",
            "shipstation_connector/static/src/js/shipstation_dashboard.js",
            "shipstation_connector/static/src/js/chatbot.js",
            "shipstation_connector/static/src/xml/chatbot.xml",
            "shipstation_connector/static/src/xml/shipstation_dashboard_templates.xml",
        ],
    },
    "images": ["static/description/icon.png"],
    "installable": True,
    "application": True,
    "auto_install": False,
}
