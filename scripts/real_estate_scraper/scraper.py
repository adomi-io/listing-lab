#!/usr/bin/env python3
import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Any

import pika
import requests
from dotenv import load_dotenv
from homeharvest import scrape_property

# Load environment variables early so LOG_LEVEL is available for logging setup
load_dotenv()


# Configure logging with LOG_LEVEL env support
def _resolve_log_level(value: str) -> int:
    if not value:
        return logging.INFO
    value = str(value).strip().lower()
    mapping = {
        'debug': logging.DEBUG,
        'info': logging.INFO,
        'warning': logging.WARNING,
        'warn': logging.WARNING,
        'error': logging.ERROR,
        'critical': logging.CRITICAL,
    }
    return mapping.get(value, logging.INFO)


LOG_LEVEL = os.getenv('LOG_LEVEL', 'info')

logging.basicConfig(
    level=_resolve_log_level(LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

# RabbitMQ connection parameters
RABBITMQ_HOST = os.getenv('RABBITMQ_HOST', 'rabbitmq')
RABBITMQ_PORT = int(os.getenv('RABBITMQ_PORT', 5672))
RABBITMQ_USER = os.getenv('RABBITMQ_USER', 'guest')
RABBITMQ_PASS = os.getenv('RABBITMQ_PASS', 'guest')
RABBITMQ_QUEUE = os.getenv('RABBITMQ_QUEUE', 'property_scrape_queue')
RABBITMQ_EXCHANGE = os.getenv('RABBITMQ_EXCHANGE', 'property_exchange')
RABBITMQ_ROUTING_KEY = os.getenv('RABBITMQ_ROUTING_KEY', 'property.scrape')

# Odoo connection parameters
ODOO_URL = os.getenv('ODOO_URL', 'http://localhost:8069')
ODOO_DB = os.getenv('ODOO_DB_NAME', 'odoo')
# Do not provide a default here — we want to ensure the user sets a real API key
ODOO_API_KEY = os.getenv('ODOO_API_KEY')


class PropertyScraper:
    def __init__(self):
        self.base_url = None
        self.headers = None
        self.channel = None
        self.connection = None

        self.connect_rabbitmq()
        self.connect_odoo()

    def connect_rabbitmq(self):
        """Connect to RabbitMQ and set up a channel"""
        logger.info(f"Connecting to RabbitMQ at {RABBITMQ_HOST}:{RABBITMQ_PORT}")
        logger.debug(
            f"RabbitMQ settings: exchange={RABBITMQ_EXCHANGE}, queue={RABBITMQ_QUEUE}, routing_key={RABBITMQ_ROUTING_KEY}, user={RABBITMQ_USER}"
        )

        # Retry connection to RabbitMQ with exponential backoff
        retry_count = 0
        max_retries = 10
        connected = False

        while not connected and retry_count < max_retries:
            try:
                credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)

                parameters = pika.ConnectionParameters(
                    host=RABBITMQ_HOST,
                    port=RABBITMQ_PORT,
                    credentials=credentials,
                    heartbeat=600
                )

                logger.debug("RabbitMQ ConnectionParameters created (heartbeat=600)")

                self.connection = pika.BlockingConnection(parameters)
                self.channel = self.connection.channel()

                logger.debug("RabbitMQ channel opened")

                # Declare exchange
                self.channel.exchange_declare(
                    exchange=RABBITMQ_EXCHANGE,
                    exchange_type='topic',
                    durable=True
                )

                logger.debug(f"Declared exchange {RABBITMQ_EXCHANGE} (type=topic, durable=True)")

                # Declare queue
                self.channel.queue_declare(
                    queue=RABBITMQ_QUEUE,
                    durable=True
                )

                logger.debug(f"Declared queue {RABBITMQ_QUEUE} (durable=True)")

                # Bind queue to exchange
                self.channel.queue_bind(
                    exchange=RABBITMQ_EXCHANGE,
                    queue=RABBITMQ_QUEUE,
                    routing_key=RABBITMQ_ROUTING_KEY
                )

                logger.debug(
                    f"Bound queue {RABBITMQ_QUEUE} to exchange {RABBITMQ_EXCHANGE} with key {RABBITMQ_ROUTING_KEY}"
                )

                connected = True
                logger.info("Successfully connected to RabbitMQ")

            except pika.exceptions.AMQPConnectionError as e:
                retry_count += 1
                wait_time = 2 ** retry_count
                logger.warning(
                    f"Failed to connect to RabbitMQ (attempt {retry_count}/{max_retries}). Retrying in {wait_time} seconds...")
                time.sleep(wait_time)

        if not connected:
            logger.error("Failed to connect to RabbitMQ after maximum retries")
            raise ConnectionError("Could not connect to RabbitMQ")

    def connect_odoo(self):
        """Connect to Odoo using JSON-2 API"""
        logger.info(f"Connecting to Odoo at {ODOO_URL}")
        logger.debug(f"Using database: {ODOO_DB}")

        try:
            if not ODOO_API_KEY:
                logger.error("ODOO_API_KEY environment variable is required")
                raise ConnectionError("ODOO_API_KEY is required")

            # Prepare headers for JSON-2 API
            self.headers = {
                'Authorization': f"bearer {ODOO_API_KEY}",
                'Content-Type': 'application/json'
            }

            # Log safe headers (mask Authorization)
            _safe_headers = dict(self.headers)

            if 'Authorization' in _safe_headers:
                _safe_headers['Authorization'] = 'bearer ****'

            logger.debug(f"Prepared headers for JSON-2 API: {_safe_headers}")

            # Add a database header if needed (for multi-database setups)
            if ODOO_DB:
                self.headers['X-Odoo-Database'] = ODOO_DB

            # Set up base URL for JSON-2 API
            self.base_url = f"{ODOO_URL}/json/2"
            logger.debug(f"Base URL set for JSON-2 API: {self.base_url}")

            # Test connection by getting current user context
            test_response = requests.post(
                f"{self.base_url}/res.users/context_get",
                headers=self.headers,
                json={},
                timeout=30
            )
            logger.debug(
                f"Odoo context_get response status={test_response.status_code}, body={test_response.text[:200]}"
            )

            if test_response.status_code != 200:
                logger.error(f"Authentication with Odoo failed: {test_response.status_code}")
                raise ConnectionError("Authentication with Odoo failed")

            logger.info(f"Connected to Odoo at {ODOO_URL} using JSON-2 API")

        except Exception as e:
            logger.error(f"Error connecting to Odoo: {str(e)}")
            raise

    def odoo_request(self, model, method, **kwargs):
        """
        Make a JSON-2 API request to Odoo
        
        Args:
            model: Model name
            method: Method name
            **kwargs: Method arguments
        
        Returns:
            Response data or None if failed
        """
        try:
            def _masked(d: dict) -> dict:
                # Mask common sensitive fields in payloads
                masked = {}

                for k, v in (d or {}).items():
                    if any(s in k.lower() for s in [
                        'password',
                        'token',
                        'apikey',
                        'api_key',
                        'authorization'
                    ]):
                        masked[k] = '****'
                    else:
                        masked[k] = v
                return masked

            # For create method, convert 'vals' to 'vals_list' as expected by JSON-2 API
            if method == 'create' and 'vals' in kwargs:
                vals = kwargs.pop('vals')
                kwargs['vals_list'] = [vals]  # Wrap single record in list

            url = f"{self.base_url}/{model}/{method}"
            logger.debug(f"Odoo request URL={url} payload={_masked(kwargs)}")

            response = requests.post(
                url,
                headers=self.headers,
                json=kwargs,
                timeout=30
            )

            if response.status_code == 200:
                logger.debug(f"Odoo response 200: {str(response.text)[:500]}")
                return response.json()
            else:
                logger.error(f"Odoo API request failed: {response.status_code} - {response.text}")
                return None
        except Exception as e:
            logger.error(f"Error making Odoo API request: {e}")
            return None

    def scrape_property(self, location: str, listing_type: str = "for_sale", **kwargs) -> List[Any]:
        """
        Scrape property data using HomeHarvest with Pydantic models
        
        Args:
            location: Location to search for properties
            listing_type: Type of listing (for_sale, for_rent, sold, pending)
            **kwargs: Additional parameters for the search
            
        Returns:
            List of Property Pydantic models
        """
        logger.info(f"Scraping property data for location: {location}, type: {listing_type}")

        logger.debug(f"HomeHarvest kwargs: {kwargs}")
        try:
            # Always use Pydantic models for return type
            kwargs['return_type'] = 'pydantic'

            # Use HomeHarvest to scrape property data
            properties = scrape_property(
                location=location,
                listing_type=listing_type,
                **kwargs
            )

            logger.info(f"Successfully scraped {len(properties)} properties")

            logger.debug(
                f"First property keys: {list(properties[0].__dict__.keys()) if properties else 'n/a'}"
            )

            return properties
        except Exception as e:
            logger.error(f"Error scraping properties: {str(e)}")
            raise

    def create_or_update_property(self, property_model: Any, record_id: Optional[int] = None) -> int:
        """
        Create or update property in Odoo using Pydantic model
        
        Args:
            property_model: Pydantic model from homeharvest
            record_id: Optional record ID for direct update
            
        Returns:
            Odoo record ID
        """
        try:
            # Convert property data to Odoo format
            odoo_property = self.map_property_to_odoo(property_model)
            property_id = record_id

            if property_id:
                logger.info(f"Using provided record_id: {property_id} for direct update")

            # If no record_id provided, check if property already exists (by property_id, mls, url, or address)
            if not property_id:
                # Check by property_id
                if 'property_id' in odoo_property and odoo_property['property_id']:
                    existing_response = self.odoo_request(
                        'real_estate.listing',
                        'search',
                        domain=[
                            ['property_id', '=', odoo_property['property_id']]
                        ]
                    )

                    if existing_response:
                        existing_ids = existing_response \
                            if isinstance(existing_response, list) \
                            else existing_response.get('result', [])

                        if existing_ids:
                            property_id = existing_ids[0]

                # Check by MLS
                if not property_id and 'mls' in odoo_property and odoo_property['mls']:
                    existing_response = self.odoo_request(
                        'real_estate.listing',
                        'search',
                        domain=[
                            ['mls', '=', odoo_property['mls']]
                        ]
                    )

                    if existing_response:
                        existing_ids = existing_response \
                            if isinstance(existing_response, list) \
                            else existing_response.get('result', [])

                        if existing_ids:
                            property_id = existing_ids[0]

                # Check by URL
                if not property_id and 'url' in odoo_property and odoo_property['url']:
                    existing_response = self.odoo_request(
                        'real_estate.listing',
                        'search',
                        domain=[
                            ['url', '=', odoo_property['url']]
                        ]
                    )

                    if existing_response:
                        existing_ids = existing_response \
                            if isinstance(existing_response, list) \
                            else existing_response.get('result', [])

                        if existing_ids:
                            property_id = existing_ids[0]

                # Check by address
                if not property_id and 'address' in odoo_property and odoo_property['address']:
                    existing_response = self.odoo_request(
                        'real_estate.listing',
                        'search',
                        domain=[
                            ['address', '=', odoo_property['address']]
                        ]
                    )

                    if existing_response:
                        existing_ids = existing_response \
                            if isinstance(existing_response, list) \
                            else existing_response.get('result', [])

                        if existing_ids:
                            property_id = existing_ids[0]

            # Extract photos, popularity, tax history, estimates, and features
            # data before removing it from odoo_property
            photos_data = None
            alt_photos_data = None
            popularity_data = None
            tax_history_data = None
            features_data = None
            estimates_data = None

            property_data = property_model.model_dump()

            if 'photos' in property_data:
                photos_data = property_data.get('photos', [])

            # Extract alt_photos from the description field
            if 'description' in property_data:
                description = property_data.get('description', {})

                logger.debug(f"Description: {description}")

                if isinstance(description, dict):
                    if 'alt_photos' in description:
                        alt_photos_data = description.get('alt_photos', [])

            if 'popularity' in property_data:
                popularity_data = property_data.get('popularity', {})
                logger.debug(f"Popularity data: {popularity_data}")

            if 'tax_history' in property_data:
                tax_history_data = property_data.get('tax_history', [])
                logger.debug(f"Tax history data: {tax_history_data}")

            if 'details' in property_data:
                features_data = property_data.get('details', [])
                logger.debug(f"Features data: {features_data}")

            if 'estimates' in property_data:
                estimates_data = property_data.get('estimates', {})
                logger.debug(f"Estimates data: {estimates_data}")

            # Create or update the property
            if property_id:
                logger.info(f"Updating existing property (ID: {property_id})")

                update_response = self.odoo_request(
                    'real_estate.listing',
                    'write',
                    ids=[property_id],
                    vals=odoo_property
                )

                logger.debug(f"Update response: {update_response}")

                if not update_response:
                    raise Exception(f"Failed to update property {property_id}")

                # Process photos if available
                if photos_data:
                    self.process_property_photos(
                        property_id,
                        photos_data,
                        alt_photos_data
                    )

                # Process popularity data if available
                if popularity_data:
                    self.process_property_popularity(
                        property_id,
                        popularity_data
                    )

                # Process tax history data if available
                if tax_history_data:
                    self.process_property_tax_history(
                        property_id,
                        tax_history_data
                    )

                # Process features data if available
                if features_data:
                    self.process_property_features(
                        property_id,
                        features_data
                    )

                # Process estimates data if available
                if estimates_data:
                    self.process_property_estimates(
                        property_id,
                        estimates_data
                    )

                return property_id
            else:
                logger.info("Creating new property")

                create_response = self.odoo_request(
                    'real_estate.listing',
                    'create',
                    vals=odoo_property
                )
                if not create_response:
                    raise Exception("Failed to create new property")

                # Handle response - it could be a list or dict
                if isinstance(create_response, list):
                    new_id = create_response[0] if create_response else None
                elif isinstance(create_response, dict):
                    new_id = create_response.get('result')
                    if isinstance(new_id, list):
                        new_id = new_id[0] if new_id else None
                else:
                    new_id = create_response

                # Process photos if available
                if photos_data:
                    self.process_property_photos(new_id, photos_data, alt_photos_data)

                # Process popularity data if available
                if popularity_data:
                    self.process_property_popularity(new_id, popularity_data)

                # Process tax history data if available
                if tax_history_data:
                    self.process_property_tax_history(new_id, tax_history_data)

                # Process features data if available
                if features_data:
                    self.process_property_features(new_id, features_data)

                # Process estimates data if available
                if estimates_data:
                    self.process_property_estimates(new_id, estimates_data)

                return new_id

        except Exception as e:
            logger.error(f"Error creating/updating property in Odoo: {str(e)}")
            raise

    def process_property_popularity(self, property_id: int, popularity_data: Dict[str, List[Dict[str, Any]]]) -> None:
        """
        Process and store property popularity metrics
        
        Args:
            property_id: Odoo property record ID
            popularity_data: Dictionary containing popularity metrics
        """
        if not popularity_data or 'periods' not in popularity_data:
            logger.info(f"No popularity data available for property {property_id}")
            return

        try:
            # Get existing popularity records for this property
            existing_response = self.odoo_request(
                'real_estate.popularity',
                'search_read',
                domain=[['property_id', '=', property_id]],
                fields=['id', 'last_n_days']
            )
            existing_records = existing_response if isinstance(existing_response, list) else existing_response.get(
                'result', []) if existing_response else []

            # Create a mapping of period days to record IDs
            existing_periods = {record['last_n_days']: record['id'] for record in existing_records}

            # Process each period in the popularity data
            for period in popularity_data.get('periods', []):
                last_n_days = period.get('last_n_days')

                # Skip if last_n_days is not available
                if not last_n_days:
                    continue

                # Prepare popularity data
                popularity_values = {
                    'property_id': property_id,
                    'last_n_days': last_n_days,
                    'views_total': period.get('views_total', 0) or 0,
                    'clicks_total': period.get('clicks_total', 0) or 0,
                    'saves_total': period.get('saves_total', 0) or 0,
                    'shares_total': period.get('shares_total', 0) or 0,
                    'leads_total': period.get('leads_total', 0) or 0,
                    'dwell_time_mean': period.get('dwell_time_mean', 0.0) or 0.0,
                    'dwell_time_median': period.get('dwell_time_median', 0.0) or 0.0,
                }

                # Update existing record or create new one
                if last_n_days in existing_periods:
                    record_id = existing_periods[last_n_days]
                    logger.info(f"Updating existing popularity record for period {last_n_days} days")

                    self.odoo_request(
                        'real_estate.popularity',
                        'write',
                        ids=[record_id],
                        vals=popularity_values
                    )
                else:
                    logger.info(f"Creating new popularity record for period {last_n_days} days")

                    self.odoo_request(
                        'real_estate.popularity',
                        'create',
                        vals=popularity_values
                    )

        except Exception as e:
            logger.error(f"Error processing popularity data: {str(e)}")
            # Continue with property creation even if popularity processing fails

    def process_property_features(self, property_id: int, features_data: List[Dict[str, Any]]) -> None:
        """
        Process and store property features
        
        Args:
            property_id: Odoo property record ID
            features_data: List of feature data dictionaries with category, parent_category, and text fields
        """
        try:
            if not features_data:
                logger.info(f"No features data to process for property {property_id}")
                return

            logger.info(f"Processing {len(features_data)} feature records for property {property_id}")

            # First, get existing feature records for this property
            existing_response = self.odoo_request(
                'real_estate.feature',
                'search_read',
                domain=[['property_id', '=', property_id]],
                fields=['id', 'category', 'parent_category']
            )
            existing_records = existing_response if isinstance(existing_response, list) else existing_response.get(
                'result', []) if existing_response else []

            # Create a map of category+parent_category -> record_id for quick lookup
            existing_features = {f"{record['parent_category']}:{record['category']}": record['id'] for record in
                                 existing_records}

            # Process each feature record
            for feature_data in features_data:
                category = feature_data.get('category', '')
                parent_category = feature_data.get('parent_category', '')
                text_items = feature_data.get('text', [])

                if not category:
                    logger.warning(f"Feature record missing category, skipping: {feature_data}")
                    continue

                # Prepare feature record data
                feature_record = {
                    'property_id': property_id,
                    'category': category,
                    'parent_category': parent_category,
                    'text_items': json.dumps(text_items)
                }

                # Check if record for this category already exists
                feature_key = f"{parent_category}:{category}"
                if feature_key in existing_features:
                    # Update existing record
                    self.odoo_request(
                        'real_estate.feature',
                        'write',
                        ids=[existing_features[feature_key]],
                        vals=feature_record
                    )
                    logger.debug(f"Updated feature record for {parent_category} - {category}")
                else:
                    # Create new record
                    create_response = self.odoo_request(
                        'real_estate.feature',
                        'create',
                        vals=feature_record
                    )
                    # Handle response - it could be a list or dict
                    if isinstance(create_response, list):
                        new_id = create_response[0] if create_response else None
                    elif isinstance(create_response, dict):
                        new_id = create_response.get('result')
                        if isinstance(new_id, list):
                            new_id = new_id[0] if new_id else None
                    else:
                        new_id = create_response
                    logger.debug(f"Created new feature record for {parent_category} - {category} with ID {new_id}")

            logger.info(f"Completed processing features for property {property_id}")

        except Exception as e:
            logger.error(f"Error processing features: {str(e)}")
            # Continue with property creation even if features processing fails

    def process_property_estimates(self, property_id: int, estimates_data: Dict[str, List[Dict[str, Any]]]) -> None:
        """
        Process and store property value estimates
        
        Args:
            property_id: Odoo property record ID
            estimates_data: Dictionary containing property value estimates
        """
        try:
            # Check if estimates data exists
            if not estimates_data or 'current_values' not in estimates_data:
                logger.info(f"No estimate data to process for property {property_id}")
                return

            current_values = estimates_data.get('current_values', [])
            if not current_values:
                logger.info(f"No current value estimates to process for property {property_id}")
                return

            logger.info(f"Processing {len(current_values)} estimate records for property {property_id}")

            # First, get existing estimate records for this property
            existing_response = self.odoo_request(
                'real_estate.estimate',
                'search_read',
                domain=[['property_id', '=', property_id]],
                fields=['id', 'date', 'source_name', 'source_type']
            )
            existing_records = existing_response if isinstance(existing_response, list) else existing_response.get(
                'result', []) if existing_response else []

            # Create a map for quick lookup of existing records
            existing_estimates = {}
            for record in existing_records:
                key = f"{record['date']}_{record['source_name']}_{record['source_type']}"
                existing_estimates[key] = record['id']

            # Process each estimate record
            for estimate_data in current_values:
                date_value = estimate_data.get('date')
                if not date_value:
                    logger.warning(f"Estimate record missing date, skipping: {estimate_data}")
                    continue

                # Convert datetime to date string
                if isinstance(date_value, datetime):
                    date_str = date_value.strftime('%Y-%m-%d')
                else:
                    date_str = str(date_value).split(' ')[0]  # Get just the date part

                # Get source information
                source = estimate_data.get('source', {})
                source_name = source.get('name', '')
                source_type = source.get('type', '')

                # Prepare estimate record data
                estimate_record = {
                    'property_id': property_id,
                    'date': date_str,
                    'estimate': estimate_data.get('estimate', 0),
                    'estimate_high': estimate_data.get('estimate_high', 0),
                    'estimate_low': estimate_data.get('estimate_low', 0),
                    'is_best_home_value': estimate_data.get('is_best_home_value', False),
                    'source_name': source_name,
                    'source_type': source_type
                }

                # Check if record already exists
                key = f"{date_str}_{source_name}_{source_type}"
                if key in existing_estimates:
                    # Update existing record
                    self.odoo_request(
                        'real_estate.estimate',
                        'write',
                        ids=[existing_estimates[key]],
                        vals=estimate_record
                    )
                    logger.debug(f"Updated estimate record for {source_name} on {date_str}")
                else:
                    # Create new record
                    create_response = self.odoo_request(
                        'real_estate.estimate',
                        'create',
                        vals=estimate_record
                    )
                    # Handle response - it could be a list or dict
                    if isinstance(create_response, list):
                        new_id = create_response[0] if create_response else None
                    elif isinstance(create_response, dict):
                        new_id = create_response.get('result')
                        if isinstance(new_id, list):
                            new_id = new_id[0] if new_id else None
                    else:
                        new_id = create_response
                    logger.debug(f"Created new estimate record for {source_name} on {date_str} with ID {new_id}")

            logger.info(f"Completed processing estimates for property {property_id}")

        except Exception as e:
            logger.error(f"Error processing estimates: {str(e)}")
            # Continue with property creation even if estimates processing fails

    def process_property_tax_history(self, property_id: int, tax_history_data: List[Dict[str, Any]]) -> None:
        """
        Process and store property tax history
        
        Args:
            property_id: Odoo property record ID
            tax_history_data: List of tax history data dictionaries
        """
        try:
            if not tax_history_data:
                logger.info(f"No tax history data to process for property {property_id}")
                return

            logger.info(f"Processing {len(tax_history_data)} tax history records for property {property_id}")

            # First, get existing tax history records for this property
            existing_response = self.odoo_request(
                'real_estate.tax_history',
                'search_read',
                domain=[['property_id', '=', property_id]],
                fields=['id', 'year']
            )
            existing_records = existing_response if isinstance(existing_response, list) else existing_response.get(
                'result', []) if existing_response else []

            # Create a map of year -> record_id for quick lookup
            existing_years = {record['year']: record['id'] for record in existing_records}

            # Process each tax history record
            for tax_data in tax_history_data:
                year = tax_data.get('year')

                if not year:
                    logger.warning(f"Tax history record missing year, skipping: {tax_data}")
                    continue

                # Prepare tax history record data
                tax_record = {
                    'property_id': property_id,
                    'year': year,
                    'tax': tax_data.get('tax', 0),
                    'assessed_year': tax_data.get('assessed_year'),
                    'value': tax_data.get('value', 0)
                }

                # Handle assessment data if present
                assessment = tax_data.get('assessment', {})
                if assessment:
                    tax_record.update({
                        'assessment_total': assessment.get('total', 0),
                        'assessment_building': assessment.get('building', 0),
                        'assessment_land': assessment.get('land', 0)
                    })

                # Add other fields if present
                if 'appraisal' in tax_data:
                    tax_record['appraisal'] = tax_data.get('appraisal', 0)

                if 'market' in tax_data:
                    tax_record['market'] = tax_data.get('market', 0)

                # Check if record for this year already exists
                if year in existing_years:
                    # Update existing record
                    self.odoo_request(
                        'real_estate.tax_history',
                        'write',
                        ids=[existing_years[year]],
                        vals=tax_record
                    )
                    logger.debug(f"Updated tax history record for year {year}")
                else:
                    # Create new record
                    create_response = self.odoo_request(
                        'real_estate.tax_history',
                        'create',
                        vals=tax_record
                    )
                    # Handle response - it could be a list or dict
                    if isinstance(create_response, list):
                        new_id = create_response[0] if create_response else None
                    elif isinstance(create_response, dict):
                        new_id = create_response.get('result')
                        if isinstance(new_id, list):
                            new_id = new_id[0] if new_id else None
                    else:
                        new_id = create_response
                    logger.debug(f"Created new tax history record for year {year} with ID {new_id}")

            logger.info(f"Completed processing tax history for property {property_id}")

        except Exception as e:
            logger.error(f"Error processing tax history: {str(e)}")
            # Continue with property creation even if tax history processing fails

    def process_property_photos(self, property_id: int, photos_data: List[Dict[str, Any]],
                                alt_photos_data: Optional[List[str]] = None) -> None:
        """
        Process and store property photos
        
        Args:
            property_id: Odoo property record ID
            photos_data: List of photo data dictionaries (preview images)
            alt_photos_data: List of alt photo URLs (detailed images)
        """
        try:
            # Check if photos_data is None or empty
            if photos_data is None:
                logger.warning(f"Photos data is None for property ID {property_id}")
                return

            if not photos_data:
                logger.info(f"No photos to process for property ID {property_id}")
                return

            logger.info(f"Processing {len(photos_data)} photos for property ID {property_id}")

            # First, get existing photos to avoid duplicates
            existing_response = self.odoo_request(
                'real_estate.photo',
                'search_read',
                domain=[['property_id', '=', property_id]],
                fields=['preview_href']
            )
            # JSON-2 responses typically wrap data under "result"; be defensive
            existing_photos: List[Dict[str, Any]] = []
            if isinstance(existing_response, list):
                existing_photos = existing_response
            elif isinstance(existing_response, dict):
                existing_photos = existing_response.get('result') or existing_response.get('records') or []

            existing_preview_hrefs = [
                p.get('preview_href') for p in existing_photos
                if isinstance(p, dict) and p.get('preview_href')
            ]

            # Process each photo
            for i, photo in enumerate(photos_data):
                # Skip None photos or photos without href
                if photo is None:
                    logger.warning(f"Skipping None photo at index {i}")
                    continue

                # Normalize photo item which can be dict | str | list/tuple
                href = ''
                title = ''
                tags = None

                if isinstance(photo, dict):
                    href = str(photo.get('href') or photo.get('url') or '')
                    title = photo.get('title', '') or ''
                    tags = photo.get('tags')
                elif isinstance(photo, str):
                    href = str(photo)
                elif isinstance(photo, (list, tuple)):
                    # Common patterns: [href], [href, tags]
                    if len(photo) > 0 and isinstance(photo[0], (str,)):
                        href = str(photo[0])
                    if len(photo) > 1:
                        tags = photo[1]
                else:
                    logger.warning(f"Unexpected photo item type at index {i}: {type(photo)} - skipping")
                    continue

                # Skip photos without href or already existing photos
                if not href or href in existing_preview_hrefs:
                    logger.info(f"Skipping photo at index {i}: missing href or already exists")
                    continue

                # Get corresponding alt_photo URL if available
                alt_photo_url = ''
                if alt_photos_data and i < len(alt_photos_data):
                    alt_val = alt_photos_data[i]
                    alt_photo_url = str(alt_val) if alt_val else ''

                # Prepare photo data
                photo_data = {
                    'property_id': property_id,
                    'preview_href': href,  # Convert HttpUrl/other to string
                    'href': str(alt_photo_url),  # Convert HttpUrl to string
                    'title': title,  # Ensure title is never None
                    'sequence': i + 1,
                    'is_primary': i == 0  # First photo is primary
                }

                # Log the photo data for debugging
                logger.info(f"Photo data for creation: {photo_data}")

                # Create photo record
                create_response = self.odoo_request(
                    'real_estate.photo',
                    'create',
                    vals=photo_data
                )
                photo_id = None
                if isinstance(create_response, (int, str)):
                    photo_id = int(create_response)
                elif isinstance(create_response, dict):
                    res = create_response.get('result') if create_response else None
                    if isinstance(res, list) and res:
                        photo_id = res[0]
                    elif isinstance(res, int):
                        photo_id = res

                # Process tags if available
                if tags:
                    # Ensure tags is not None and is a list
                    if isinstance(tags, list):
                        self.process_photo_tags(photo_id, tags)
                    else:
                        logger.warning(f"Tags for photo {photo_id} is not a list: {type(tags)}")

            logger.info(f"Successfully processed photos for property ID {property_id}")

        except Exception as e:
            logger.error(f"Error processing property photos: {str(e)}")
            # Continue with property creation/update even if photo processing fails

    def process_photo_tags(self, photo_id: int, tags_data: List[Dict[str, Any]]) -> None:
        """
        Process and store photo tags
        
        Args:
            photo_id: Odoo photo record ID
            tags_data: List of tag data dictionaries
        """
        try:
            # Ensure tags_data is not None
            if tags_data is None:
                logger.warning(f"Tags data is None for photo ID {photo_id}")
                return

            logger.info(f"Processing {len(tags_data)} tags for photo ID {photo_id}")
            tag_ids = []

            for tag_data in tags_data:
                if tag_data is None:
                    continue

                # Handle different tag data formats
                if isinstance(tag_data, dict):
                    tag_label = tag_data.get('label', '')
                elif isinstance(tag_data, str):
                    tag_label = tag_data
                else:
                    logger.warning(f"Unexpected tag data type: {type(tag_data)}, value: {tag_data}")
                    continue

                if not tag_label:
                    continue

                # Check if tag already exists
                existing_response = self.odoo_request(
                    'real_estate.photo.tag',
                    'search',
                    domain=[['name', '=', tag_label]]
                )
                existing_tags = existing_response if isinstance(existing_response, list) else existing_response.get(
                    'result', []) if existing_response else []

                if existing_tags:
                    tag_id = existing_tags[0]
                else:
                    # Create new tag
                    create_response = self.odoo_request(
                        'real_estate.photo.tag',
                        'create',
                        vals={'name': tag_label}
                    )
                    tag_id = create_response if isinstance(create_response, (int, str)) else create_response.get(
                        'result') if create_response else None

                tag_ids.append(tag_id)

            # Update photo with tags
            if tag_ids:
                # Filter out any None values that might have slipped in
                filtered_tag_ids = [tag_id for tag_id in tag_ids if tag_id is not None]

                if filtered_tag_ids:
                    logger.info(f"Updating photo {photo_id} with {len(filtered_tag_ids)} tags")
                    self.odoo_request(
                        'real_estate.photo',
                        'write',
                        ids=[photo_id],
                        vals={'tag_ids': [(6, 0, filtered_tag_ids)]}
                    )
                else:
                    logger.warning(f"No valid tag IDs found for photo {photo_id} after filtering")

        except Exception as e:
            logger.error(f"Error processing photo tags: {str(e)}")
            # Continue with photo creation even if tag processing fails

    def process_property_tags(self, tags_data: List[str]) -> List[int]:
        try:
            if not tags_data:
                return []

            logger.info(f"Processing {len(tags_data)} property tags")
            tag_ids = []

            for api_name in tags_data:
                if not api_name or not isinstance(api_name, str):
                    continue

                # First, try to look up tag by api_name
                existing_response = self.odoo_request(
                    'real_estate.tag',
                    'search',
                    domain=[['api_name', '=', api_name]]
                )
                existing_tags = existing_response if isinstance(existing_response, list) else existing_response.get(
                    'result', []) if existing_response else []

                if existing_tags:
                    # Tag exists, use it
                    tag_id = existing_tags[0]
                    logger.debug(f"Found existing tag for api_name '{api_name}': {tag_id}")
                else:
                    # Tag doesn't exist, create new one
                    # Convert api_name to display name (e.g., 'community_gym' -> 'Community Gym')
                    display_name = ' '.join(word.capitalize() for word in api_name.split('_'))

                    tag_data = {
                        'name': display_name,
                        'api_name': api_name,
                        'tag_type': 'listing',  # mark scraper-generated tags
                    }

                    create_response = self.odoo_request(
                        'real_estate.tag',
                        'create',
                        vals=tag_data
                    )

                    if len(create_response) > 0:
                        tag_id = create_response[0]

                    logger.info(f"Created new tag '{display_name}' (api_name: '{api_name}') with ID {tag_id}")

                if tag_id:
                    tag_ids.append(tag_id)

            logger.info(f"Processed property tags, returning {len(tag_ids)} tag IDs")
            return tag_ids

        except Exception as e:
            logger.error(f"Error processing property tags: {str(e)}")
            return []

    def map_property_to_odoo(self, property_model: Any) -> Dict[str, Any]:
        """
        Map HomeHarvest Pydantic model to Odoo model fields

        Args:
            property_model: Pydantic model from homeharvest
            
        Returns:
            Dictionary with Odoo field mappings
        """
        # Convert Pydantic model to dict for easier access
        # We use the model's model_dump() method which handles nested models
        prop = property_model.model_dump()

        # Extract address components if available
        address_components = {}

        if 'address' in prop:
            # Handle case where key exists but value is None
            address = prop.get('address') or {}

            address_components = {
                'street': address.get('street', ''),
                'unit': address.get('unit', ''),
                'city': address.get('city', ''),
                'state': address.get('state', ''),
                'zip_code': address.get('zip', ''),
                'formatted_address': address.get('formatted_address', '')
            }

        # Extract description components if available
        description_components = {}

        if 'description' in prop:
            # Handle case where key exists but value is None
            desc = prop.get('description') or {}

            description_components = {
                'beds': desc.get('beds', 0),
                'baths_full': desc.get('baths_full', 0),
                'baths_half': desc.get('baths_half', 0),
                'sqft': desc.get('sqft', 0),
                'lot_sqft': desc.get('lot_sqft', 0),
                'year_built': desc.get('year_built', 0),
                'stories': desc.get('stories', 0),
                'garage': desc.get('garage', 0),
                'style': desc.get('style', ''),
                'text': desc.get('text', '')
            }

        # Extract advertiser information from nested structure
        advertisers = prop.get('advertisers', {}) or {}
        agent = advertisers.get('agent', {}) or {}
        broker = advertisers.get('broker', {}) or {}
        office = advertisers.get('office', {}) or {}

        agent_phones = agent.get('phones') or []
        office_phones = office.get('phones') or []

        def first_phone(phones: list) -> str:
            if not phones:
                return ''
            return (phones[0] or {}).get('number', '') or ''

        agent_phone = first_phone(agent_phones)

        # Map HomeHarvest fields to Odoo fields
        odoo_property = {
            # Basic Information
            'property_id': prop.get('property_id', ''),
            'mls': prop.get('mls', ''),
            'mls_id': prop.get('mls_id', ''),
            'mls_status_raw': prop.get('mls_status', ''),

            # Address Components
            'address': self.format_address(address_components),
            'street': address_components.get('street', ''),
            'street_number': address_components.get('street_number', ''),
            'street_direction': address_components.get('street_direction', ''),
            'street_name': address_components.get('street_name', ''),
            'street_suffix': address_components.get('street_suffix', ''),
            'address_full_line': address_components.get('full_line', ''),
            'unit': address_components.get('unit', ''),
            'city': address_components.get('city', ''),
            'state': address_components.get('state', ''),
            'zip_code': address_components.get('zip_code', ''),
            'county': prop.get('county', ''),
            'neighborhoods': json.dumps(prop.get('neighborhoods', [])) if prop.get('neighborhoods') else '',

            # Location Information
            'latitude': float(prop.get('latitude', 0.0) or 0.0),
            'longitude': float(prop.get('longitude', 0.0) or 0.0),
            'fips_code': prop.get('fips_code', ''),
            'parcel_number': prop.get('parcel_number', ''),

            # Price Information
            'price': float(prop.get('list_price', 0) or 0),
            'list_price_min': float(prop.get('list_price_min', 0) or 0),
            'list_price_max': float(prop.get('list_price_max', 0) or 0),
            'sold_price': float(prop.get('sold_price', 0) or 0),
            'last_sold_price': float(prop.get('last_sold_price', 0) or 0),
            'estimated_monthly_rental': float(prop.get('estimated_monthly_rental', 0) or 0),

            # Property Description
            # Ensure property_type is a serializable value
            'property_type': self.map_property_type(description_components.get('style', '')),
            # property_type_raw removed in Odoo model
            'listing_description': description_components.get('text', ''),
            'description_title': description_components.get('name', ''),
            'bedrooms': int(description_components.get('beds', 0) or 0),
            'baths_full': int(description_components.get('baths_full', 0) or 0),
            'baths_half': int(description_components.get('baths_half', 0) or 0),
            'sqft': int(description_components.get('sqft', 0) or 0),
            'lot_sqft': int(description_components.get('lot_sqft', 0) or 0),
            'stories': float(description_components.get('stories', 0.0) or 0.0),
            'garage': int(description_components.get('garage', 0) or 0),
            'parking': json.dumps(prop.get('parking', {})) if prop.get('parking') else '',
            'year_built': int(description_components.get('year_built', 0) or 0),
            # new_construction removed; we set is_new_construction from flags below

            # Status and Dates
            'market_status': self.map_status(prop.get('status', '')),
            'listing_date': self.format_datetime(prop.get('list_date', '')),
            'pending_date': self.format_datetime(prop.get('pending_date', '')),
            'sold_date': self.format_datetime(prop.get('last_sold_date', '')),
            'days_on_mls': int(prop.get('days_on_mls', 0) or 0),

            # Financial Information
            # annual_tax/assessed_value are related from latest tax in Odoo
            'hoa_fee': float(prop.get('hoa_fee', 0) or 0),

            # URL and Media
            'url': str(prop.get('property_url', '')),
            # primary_photo is related to primary_image_id in Odoo

            # Agent/Broker Information
            'agent_name': agent.get('name', ''),
            'agent_phone': agent_phone,
            'agent_email': agent.get('email', ''),
            'agent_uuid': agent.get('uuid', ''),
            'agent_state_license': agent.get('state_license', ''),
            'broker_name': broker.get('name', ''),
            'broker_uuid': broker.get('uuid', ''),
            'office_name': office.get('name', ''),
            'office_uuid': office.get('uuid', ''),
            'office_email': office.get('email', ''),
            # office_phones removed in Odoo model

            # Tax Record Information
            # Use `or {}` so that if the key exists but is None, we still get an empty dict
            'tax_record_apn': (prop.get('tax_record') or {}).get('apn', ''),
            'tax_record_cl_id': (prop.get('tax_record') or {}).get('cl_id', ''),
            'tax_record_last_update_date': self.format_datetime(
                (prop.get('tax_record') or {}).get('last_update_date', '')),
            'tax_record_public_record_id': (prop.get('tax_record') or {}).get('public_record_id', ''),
            'tax_record_tax_parcel_id': (prop.get('tax_record') or {}).get('tax_parcel_id', ''),

            # Property Flags
            # Same safeguard for flags
            'is_coming_soon': bool((prop.get('flags') or {}).get('is_coming_soon', False)),
            'is_contingent': bool((prop.get('flags') or {}).get('is_contingent', False)),
            'is_foreclosure': bool((prop.get('flags') or {}).get('is_foreclosure', False)),
            'is_new_construction': bool((prop.get('flags') or {}).get('is_new_construction', False)),
            'is_new_listing': bool((prop.get('flags') or {}).get('is_new_listing', False)),
            'is_pending': bool((prop.get('flags') or {}).get('is_pending', False)),
            'is_price_reduced': bool((prop.get('flags') or {}).get('is_price_reduced', False)),

            # Additional Information
            'pet_policy': json.dumps(prop.get('pet_policy', {})) if prop.get('pet_policy') else '',
            'terms': prop.get('terms', ''),
            # nearby schools handled via M2M linking below
            'open_houses': json.dumps(prop.get('open_houses', [])) if prop.get('open_houses') else '',
            'units': json.dumps(prop.get('units', [])) if prop.get('units') else '',
            'current_estimates': json.dumps(
                self.convert_datetimes_for_json(
                    prop.get('current_estimates', {})
                )
            ) if prop.get('current_estimates') else '',
            'estimates': json.dumps(
                self.convert_datetimes_for_json(
                    prop.get('estimates', {})
                )
            ) if prop.get('estimates') else '',
        }

        # Add property tags if they exist
        property_tags = prop.get('tags', [])

        if property_tags:
            odoo_property['property_tags'] = json.dumps(property_tags)

            # Process property tags to create/lookup tag records
            tag_ids = self.process_property_tags(property_tags)

            if tag_ids:
                odoo_property['listing_tag_ids'] = [(6, 0, tag_ids)]

        # Nearby schools: create/lookup schools and link via M2M
        nearby = prop.get('nearby_schools') or []

        if nearby:
            school_ids = []
            for name in nearby:
                if not name:
                    continue
                # find existing school by name
                existing = self.odoo_request(
                    'real_estate.school',
                    'search',
                    domain=[["name", "=", name]],
                    limit=1,
                )
                sid = existing[0] if existing else None
                if not sid:
                    created = self.odoo_request(
                        'real_estate.school',
                        'create',
                        vals={'name': name}
                    )
                    if created:
                        sid = created[0]
                if sid:
                    school_ids.append(sid)
            if school_ids:
                odoo_property['nearby_school_ids'] = [(6, 0, school_ids)]

        # Filter out None values and ensure all values are serializable
        filtered_property = {}

        for k, v in odoo_property.items():
            if v is None:
                continue

            # Check if value is a built-in function or method
            if callable(v) or isinstance(v, type) or hasattr(v, '__call__'):
                logger.warning(f"Skipping non-serializable value for key {k}: {type(v)}")
                continue

            # Try to convert complex objects to strings
            try:
                # Convert datetime objects before testing JSON serialization
                test_value = self.convert_datetimes_for_json(v) if isinstance(v, (datetime, dict, list)) else v
                # Test if the value is JSON serializable
                json.dumps(test_value)
                filtered_property[k] = test_value
            except (TypeError, OverflowError):
                # If not serializable, try to convert to string
                try:
                    filtered_property[k] = str(v)
                    logger.warning(f"Converted non-serializable value for key {k} to string: {filtered_property[k]}")
                except Exception as e:
                    logger.warning(f"Skipping non-serializable value for key {k}: {type(v)}, error: {str(e)}")
                    continue

        return filtered_property

    def format_address(self, address_components: Dict[str, str]) -> str:
        """
        Format the address from address components
        
        Args:
            address_components: Dictionary with address components
            
        Returns:
            Formatted address string
        """
        # Try to get formatted address first
        if 'formatted_address' in address_components and address_components['formatted_address']:
            return address_components['formatted_address']

        # Otherwise build from components
        address_parts = []

        if 'street' in address_components and address_components['street']:
            address_parts.append(address_components['street'])

        if 'unit' in address_components and address_components['unit']:
            address_parts.append(address_components['unit'])

        city_state_zip = []

        if 'city' in address_components and address_components['city']:
            city_state_zip.append(address_components['city'])

        if 'state' in address_components and address_components['state']:
            if city_state_zip:
                city_state_zip.append(f", {address_components['state']}")
            else:
                city_state_zip.append(address_components['state'])

        if 'zip_code' in address_components and address_components['zip_code']:
            city_state_zip.append(f" {address_components['zip_code']}")

        if city_state_zip:
            address_parts.append(''.join(city_state_zip))

        return '\n'.join(address_parts) if address_parts else ''

    def format_datetime(self, dt_value: Any) -> str:
        """
        Format datetime objects to Odoo-compatible datetime strings
        
        Args:
            dt_value: Datetime object or string
            
        Returns:
            Datetime string in format '%Y-%m-%d %H:%M:%S' or empty string if None
        """
        if not dt_value:
            return ''

        # If it's a datetime object, convert to Odoo format
        if isinstance(dt_value, datetime):
            formatted = dt_value.strftime('%Y-%m-%d %H:%M:%S')
            logger.info(f"Converted datetime object to Odoo format: {formatted}")
            return formatted

        # If it's a string, try to parse it and convert to Odoo format
        if isinstance(dt_value, str):
            try:
                # Handle ISO format with timezone (e.g., '2025-07-16T01:04:52+00:00')
                if 'T' in dt_value and ('+' in dt_value or 'Z' in dt_value):
                    # Parse ISO format string to datetime object
                    dt_obj = datetime.fromisoformat(dt_value.replace('Z', '+00:00'))
                    # Convert to Odoo format
                    formatted = dt_obj.strftime('%Y-%m-%d %H:%M:%S')
                    logger.info(f"Converted ISO datetime '{dt_value}' to Odoo format: {formatted}")
                    return formatted
                logger.info(f"Using datetime string as-is: {dt_value}")
                return dt_value
            except (ValueError, TypeError) as e:
                logger.warning(f"Failed to parse datetime string '{dt_value}': {e}")
                return ''

        # For any other type, try to convert to string
        try:
            result = str(dt_value)
            logger.info(f"Converted {type(dt_value)} to string: {result}")
            return result
        except Exception as e:
            logger.warning(f"Failed to convert datetime value to string: {e}")
            return ''

    def convert_datetimes_for_json(self, obj: Any) -> Any:
        """
        Recursively convert datetime objects to strings for JSON serialization
        
        Args:
            obj: Object that may contain datetime objects
            
        Returns:
            Object with datetime objects converted to strings
        """
        if isinstance(obj, datetime):
            return obj.strftime('%Y-%m-%d %H:%M:%S')
        elif isinstance(obj, dict):
            return {k: self.convert_datetimes_for_json(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self.convert_datetimes_for_json(item) for item in obj]
        else:
            return obj

    def map_status(self, status: Any) -> str:
        """
        Map HomeHarvest status to Odoo status
        
        Args:
            status: HomeHarvest status (can be string or enum)
            
        Returns:
            Odoo status
        """
        try:
            # Handle case where status is an enum or object instead of string
            if not isinstance(status, str) and hasattr(status, '__str__'):
                try:
                    status = str(status)
                except Exception as e:
                    logger.warning(f"Failed to convert status to string: {e}")
                    return 'off_market'

            if not isinstance(status, str):
                return 'off_market'

            status_map = {
                'for_sale': 'active',
                'for_rent': 'active',
                'pending': 'contingent',
                'contingent': 'contingent',
                'sold': 'off_market',
            }

            return status_map.get(status.lower(), 'off_market')
        except Exception as e:
            logger.warning(f"Error in map_status: {e}")
            return 'interested'  # Safe default

    def map_property_type(self, style: Any) -> str:
        """
        Map HomeHarvest style to Odoo property_type
        
        Args:
            style: HomeHarvest property style (can be string or PropertyType enum)
            
        Returns:
            Odoo property_type as string
        """
        try:
            if not style:
                return 'single_family'  # Default value instead of False

            # Handle case where style is an enum or object instead of string
            if hasattr(style, '__str__'):
                try:
                    style = str(style)
                except Exception as e:
                    logger.warning(f"Failed to convert style to string: {e}")
                    return 'single_family'

            # Convert to lowercase for case-insensitive matching
            style_lower = style.lower() if isinstance(style, str) else ""

            # Map HomeHarvest style values to Odoo property_type values
            property_type_map = {
                'single_family': 'single_family',
                'single family': 'single_family',
                'singlefamily': 'single_family',
                'single-family': 'single_family',
                'multi_family': 'multi_family',
                'multi family': 'multi_family',
                'multifamily': 'multi_family',
                'multi-family': 'multi_family',
                'condo': 'condos',
                'condos': 'condos',
                'condominium': 'condos',
                'condo/townhome': 'condo_townhome',
                'condo_townhome': 'condo_townhome',
                'condo/townhouse': 'condo_townhome',
                'townhome': 'townhomes',
                'townhouse': 'townhomes',
                'townhomes': 'townhomes',
                'townhouses': 'townhomes',
                'duplex': 'duplex_triplex',
                'triplex': 'duplex_triplex',
                'duplex/triplex': 'duplex_triplex',
                'duplex_triplex': 'duplex_triplex',
                'farm': 'farm',
                'ranch': 'farm',
                'land': 'land',
                'lot': 'land',
                'mobile': 'mobile',
                'mobile home': 'mobile',
                'manufactured': 'mobile'
            }

            # Try direct match first
            if style_lower in property_type_map:
                return property_type_map[style_lower]

            # Try partial matching for values not in the map
            for key, value in property_type_map.items():
                if key in style_lower:
                    return value

            # Default to single_family if no match found
            return 'single_family'
        except Exception as e:
            logger.warning(f"Error in map_property_type: {e}")
            return 'single_family'  # Safe default

    def process_message(self, ch, method, properties, body):
        """
        Process incoming RabbitMQ message
        
        Args:
            ch: Channel
            method: Method
            properties: Properties
            body: Message body
        """
        try:
            logger.info(f"Received message: {body}")
            logger.debug(f"Delivery info: method={method}, properties={properties}")

            message = json.loads(body)

            # Extract scraping parameters from message
            location = message.get('location')
            listing_type = message.get('listing_type', 'for_sale')
            record_id = message.get('record_id')  # Extract record_id if provided
            logger.debug(f"Parsed message: location={location}, listing_type={listing_type}, record_id={record_id}")

            if record_id:
                logger.info(f"Record ID provided: {record_id}. Will update this specific record.")
                message['limit'] = 1
            else:
                logger.info("No record ID provided. Will search for existing record or create new one.")

            if not location:
                logger.error("No location provided in message")
                ch.basic_ack(delivery_tag=method.delivery_tag)
                return

            # Extract additional parameters and filter out unsupported parameters like 'source_url'
            kwargs = {
                k: v for k, v in message.items()
                if k not in [
                    'location',
                    'listing_type',
                    'record_id',
                    'source_url'
                ]
            }
            logger.debug(f"Scrape kwargs after filtering: {kwargs}")

            # Log the parameters being passed to scrape_property
            logger.info(
                f"Calling scrape_property with: location={location}, listing_type={listing_type}, kwargs={kwargs}")

            # Scrape property data using Pydantic models
            properties = self.scrape_property(
                location,
                listing_type,
                **kwargs
            )

            # Process each property
            property_ids = []

            if record_id and len(properties) > 1:
                logger.error("There was more than one item at this address, so we will take only the top result")
                property_ids = [properties[0]]

            for property_model in properties:
                property_id = self.create_or_update_property(property_model, record_id)
                property_ids.append(property_id)

            logger.info(f"Successfully processed {len(property_ids)} properties")

            # Acknowledge message
            ch.basic_ack(delivery_tag=method.delivery_tag)

        except json.JSONDecodeError:
            logger.error("Invalid JSON in message")
            ch.basic_ack(delivery_tag=method.delivery_tag)
        except Exception as e:
            logger.error(f"Error processing message: {str(e)}")
            # Remove the message from the queue, something went wrong.
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    def start_consuming(self):
        """Start consuming messages from RabbitMQ"""
        logger.info(f"Starting to consume messages from queue: {RABBITMQ_QUEUE}")
        logger.debug("Configuring basic_qos with prefetch_count=1 and registering consumer callback")

        # Set up consumer
        self.channel.basic_qos(prefetch_count=1)

        self.channel.basic_consume(
            queue=RABBITMQ_QUEUE,
            on_message_callback=self.process_message
        )

        # Start consuming
        try:
            self.channel.start_consuming()
        except KeyboardInterrupt:
            logger.info("Stopping consumer")
            self.channel.stop_consuming()
        finally:
            if self.connection.is_open:
                self.connection.close()
                logger.debug("RabbitMQ connection closed")


if __name__ == "__main__":
    logger.info("Starting Property Scraper")

    # Guard: Require ODOO_API_KEY to be set and non-empty
    if not ODOO_API_KEY or not str(ODOO_API_KEY).strip():
        msg = """
Scraper is not running
============================================================
Missing Odoo API Key (ODOO_API_KEY).

If this is your first time running Listing Lab, this is a normal error.
You need to give the scraper an API key, which you can only do once the web application
is running.

To generate an API key in Listing Lab:
  1) Open your Listing Lab instance in a web browser (by default: http://localhost:8069).
  2) Click your profile avatar in the top-right.
  3) Open 'My Preferences'.
  4) Click 'Settings'.
  5) Open the 'Security' tab.
  6) Click 'Add API Key'.
  7) Enter your password (default is 'admin' on a fresh install).
  8) Set the key duration to 'Persistent Key' and give it a name.
  9) Copy the generated key.

Then update your .env file to include:
  ODOO_API_KEY=your_generated_key_here

Or, in your docker-compose.yml, set the API key in the environment options
real_estate_scraper:
    ...
    environment:
        ODOO_API_KEY: "your_generated_key_here"

After updating, restart the application.

Read more: https://github.com/adomi-io/listing-lab
============================================================
        """

        print(msg)
        # Exit gracefully without stack trace so users can follow instructions
        sys.exit(0)

    # Create and start the scraper
    scraper = PropertyScraper()
    scraper.start_consuming()
