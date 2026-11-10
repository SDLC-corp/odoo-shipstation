{
    "name": "ShipStation Odoo Connector",
    "version": "16.0.1.1.0",
    "summary": "Connect ShipStation with Odoo for orders, shipments, labels, tracking, inventory, and reports",
    "description": """
    ShipStation Odoo Connector
    ==========================

    The ShipStation Odoo Connector by SDLC Corp helps businesses automate and
    centralize shipping, fulfillment, order management, and inventory workflows
    between Odoo and ShipStation.

    This connector supports bidirectional synchronization between Odoo and
    ShipStation, allowing businesses to manage orders, products, customers,
    shipments, inventory, tracking information, and shipping operations from
    one unified ERP system.

    =================================================
    Main Features
    =================================================

    ShipStation to Odoo Synchronization
    -----------------------------------
    * Import ShipStation orders into Odoo
    * Synchronize customers from ShipStation to Odoo
    * Import products and categories
    * Synchronize inventory data
    * Import shipment and tracking information
    * Automatically create and update fulfillment records
    * Maintain shipping visibility inside Odoo

    Odoo to ShipStation Synchronization
    -----------------------------------
    * Export Odoo orders to ShipStation
    * Synchronize products and product updates
    * Review inventory discrepancies without ever changing Odoo stock
    * Link ShipStation shipments and tracking to the matching delivery order
    * Preserve inventory safety with read-only reconciliation

    Shipping & Fulfillment Management
    ---------------------------------
    * Shipment import with tracking, carrier and voided-label status
    * Shipping-rate comparison and carrier / package presets
    * Single-order label purchase with an explicit confirmation, a durable
      "request sent" guard and no automatic retry (never bulk or background)
    * Shipping Workbench and Visual Exception Board for unlinked shipments
    * Fulfillment preparation batches and manual return-case tracking
    * Operations Home dashboard, guided setup and webhook inbox with retry

    Operational Features
    --------------------
    * Multi-instance ShipStation configuration
    * Automated cron-based synchronization
    * Dashboard for fulfillment monitoring
    * Sync logs and reporting
    * Field mapping configuration
    * Error tracking and synchronization history
    * API-based communication

    Technical Highlights
    --------------------
    * Built with standard Odoo architecture
    * Odoo ORM-based implementation
    * Role-based access (Reader, Manager, Admin) and multi-company isolation
    * Automated test suite (database-backed and offline)
    * Upgrade-friendly module structure
    * Tested on Odoo 16 Community, on-premise (Enterprise and Odoo.sh not yet verified)

    Business Benefits
    -----------------
    * Reduce manual shipping operations
    * Improve fulfillment visibility
    * Centralize shipping management
    * Maintain inventory accuracy
    * Reduce synchronization errors
    * Improve warehouse efficiency
    * Streamline order fulfillment workflows

    Supported Odoo Modules
    ----------------------
    * Sales
    * Inventory
    * Delivery
    * Contacts
    * Discuss (mail)
    * Web

    External Python Dependency
    --------------------------
    * requests

    Developed and Maintained By
    ---------------------------
    SDLC Corp
    https://sdlccorp.com/
    """,
    "category": "Inventory/Delivery",
    "author": "SDLC Corp",
    "website": "https://sdlccorp.com/products/odoo-shipstation-connector/",
    "maintainer": "SDLC Corp",
    "support": "sales@sdlccorp.com",
    'price': 19.99,
    'currency': 'USD',
    "license": "OPL-1",
    "external_dependencies": {"python": ["requests"]},
    "depends": [
        "base",
        "web",
        "mail",
        "sale_management",
        "stock",
        "delivery",
        "contacts",
    ],
    "data": [
        "security/shipstation_security.xml",
        "security/ir.model.access.csv",
        "security/shipstation_record_rules.xml",
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
        "views/shipstation_workbench_views.xml",
        "views/shipstation_s06_shipping_views.xml",
        "views/shipstation_s07_fulfillment_returns_views.xml",
        "views/shipstation_s08_operations_views.xml",
        "views/menu.xml",
        "views/shipstation_webhook_inbox_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "shipstation_connector/static/src/css/shipstation_dashboard.css",
            "shipstation_connector/static/src/css/shipstation_operations_home.css",
            "shipstation_connector/static/src/scss/chatbot.scss",
            "shipstation_connector/static/src/js/shipstation_dashboard.js",
            "shipstation_connector/static/src/js/shipstation_operations_home.js",
            "shipstation_connector/static/src/js/chatbot.js",
            "shipstation_connector/static/src/xml/chatbot.xml",
            "shipstation_connector/static/src/xml/shipstation_dashboard_templates.xml",
            "shipstation_connector/static/src/xml/shipstation_operations_home.xml",
        ],
    },
    "images": ["static/description/banner.gif"],
    "installable": True,
    "application": True,
    "auto_install": False,
}
