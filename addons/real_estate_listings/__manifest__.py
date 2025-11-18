{
    'name': 'Adomi - Listing Lab',
    'version': '1.0',
    'summary': 'Track and manage real estate property listings',
    'description': """
                                      A simple, friendly place to keep track of homes you’re looking at. 
                                      Save listings, add notes, tag photos, and share the short list with 
                                      the people who matter. Listing Lab also keeps an eye on changes—like 
                                      price cuts and status updates—so your view of a property stays current 
                                      without constant refreshing.
                                      """,
    'category': 'Real Estate',
    'author': 'Adomi Software, LLC',
    'website': 'https://github.com/adomi-io/listing-lab',
    'depends': [
        'base',
        'mail',
        'web',
        'contacts',
        'account',
        'unovis_charts'
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/saved_search_data.xml',
        'data/cron_data.xml',
        'views/view_real_estate.xml',
        'views/view_saved_search.xml',
        'views/view_photo.xml',
        'views/view_popularity.xml',
        'views/view_tax_history.xml',
        'views/view_features.xml',
        'views/view_estimate.xml',
        'views/menu_real_estate.xml',
    ],
    "images": [
        "static/description/icon.png",
    ],
    'assets': {
        'web.assets_backend': [
            'real_estate_listings/static/src/scss/real_estate.scss',
            'real_estate_listings/static/src/js/monetary_no_cents_widget.js',
            'real_estate_listings/static/src/js/integer_no_comma_widget.js',
            'real_estate_listings/static/src/components/listing_summary/listing_summary.js',
            'real_estate_listings/static/src/components/listing_summary/listing_summary.scss',
            'real_estate_listings/static/src/components/listing_summary/listing_summary.xml',
            'real_estate_listings/static/src/components/listing_features/listing_features.js',
            'real_estate_listings/static/src/components/listing_features/listing_features.scss',
            'real_estate_listings/static/src/components/listing_features/listing_features.xml',
            'real_estate_listings/static/src/components/listing_bus_listener/listing_bus_listener.js',
            'real_estate_listings/static/src/components/listing_bus_listener/listing_bus_listener.xml',
        ],
    },
    'demo': [],
    'installable': True,
    'application': True,
    'auto_install': True,
    'license': 'LGPL-3',
}
