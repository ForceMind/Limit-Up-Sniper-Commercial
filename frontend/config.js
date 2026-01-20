// Determine API Base URL
// If served from same origin (production), use relative path (empty string)
// If served from file or different origin (development), use localhost
const API_BASE_URL = (location.protocol === 'file:' || location.hostname === 'localhost' && location.port !== '8000') 
    ? 'http://localhost:8000' 
    : '';
