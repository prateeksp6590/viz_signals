import logging
import os

level = getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO)

logging.basicConfig(
    level=level,
    format='%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

logger = logging.getLogger('viz_signals')
