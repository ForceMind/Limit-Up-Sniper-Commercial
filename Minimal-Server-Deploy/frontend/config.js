// Determine API Base URL
// If served from same origin (production), use relative path (empty string)
// If served from file or different origin (development), use localhost
const getApiBaseUrl = () => {
    // If user configured a global API_PREFIX, use it
    if (window.API_PREFIX) return window.API_PREFIX;

    const protocol = location.protocol;
    const hostname = location.hostname;
    const port = location.port;

    if (protocol === 'file:') {
        return 'http://localhost:8000';
    }

    if (hostname === 'localhost' || hostname === '127.0.0.1') {
        if (port !== '8000') {
            return 'http://localhost:8000';
        }
    }

    return '';
};

// Admin Config
const ADMIN_BASE_PATH = '/admin'; // Default admin path

const API_BASE_URL = getApiBaseUrl();
