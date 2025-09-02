#!/usr/bin/env python3
"""
Episode Link Extractor Server - Headless Browser Version
Uses Playwright to execute JavaScript and extract dynamically generated Google Drive links
"""

import asyncio
import aiohttp
from aiohttp import web
from bs4 import BeautifulSoup
import json
import logging
import os
from urllib.parse import urljoin, urlparse
import time
from datetime import datetime
import re
import aiofiles
from playwright.async_api import async_playwright

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class EpisodeLinkExtractor:
    def __init__(self):
        self.session = None
        self.results_file = "extracted_links.json"
        self.results = []
        # List of known ad/redirector domains to block
        self.ad_domains = [
            "ghastlyejection.com", "adf.ly", "ad-maven.com", "adsterra.com", 
            "propellerads.com", "outbrain.com", "taboola.com"
        ]
        
    async def __aenter__(self):
        connector = aiohttp.TCPConnector(limit=10)
        timeout = aiohttp.ClientTimeout(total=120)  # Increased timeout for browser operations
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate',
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
            }
        )
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    async def fetch_page(self, url):
        """Fetch a webpage and return the HTML content"""
        try:
            async with self.session.get(url) as response:
                if response.status == 200:
                    content = await response.text()
                    logger.info(f"Successfully fetched: {url}")
                    return content
                else:
                    logger.error(f"Failed to fetch {url}: Status {response.status}")
                    return None
        except Exception as e:
            logger.error(f"Error fetching {url}: {str(e)}")
            return None
    
    def extract_episode_links(self, html_content, base_url):
        """Extract single episode links from the main page"""
        soup = BeautifulSoup(html_content, 'html.parser')
        episode_links = []
        
        # Look for episode links (avoiding season zip links)
        links = soup.find_all('a', href=True)
        
        for link in links:
            href = link.get('href')
            text = link.get_text(strip=True)
            
            # Filter for episode links (not season zip)
            if href and 'Episode' in text and 'Season' not in text and 'Zip' not in text:
                full_url = urljoin(base_url, href)
                episode_links.append({
                    'episode_name': text,
                    'episode_url': full_url
                })
        
        # Alternative: Look for specific episode patterns
        if not episode_links:
            # Look for links containing episode patterns
            for link in links:
                href = link.get('href')
                text = link.get_text(strip=True)
                
                if href and re.search(r'episode\s*\d+', text, re.IGNORECASE):
                    if 'zip' not in text.lower() and 'season' not in text.lower():
                        full_url = urljoin(base_url, href)
                        episode_links.append({
                            'episode_name': text,
                            'episode_url': full_url
                        })
        
        logger.info(f"Found {len(episode_links)} episode links")
        return episode_links
    
    async def extract_instant_dl_link(self, episode_url):
        """Extract the Instant DL link from episode page"""
        html_content = await self.fetch_page(episode_url)
        if not html_content:
            return None
            
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Look for Instant DL link
        instant_dl_link = soup.find('a', class_=['btn', 'btn-danger'])
        if instant_dl_link and 'Instant DL' in instant_dl_link.get_text():
            return instant_dl_link.get('href')
        
        # Alternative search patterns
        instant_dl_link = soup.find('a', string=re.compile('Instant DL', re.IGNORECASE))
        if instant_dl_link:
            return instant_dl_link.get('href')
            
        # Look for any link with instant or DL in the text
        for link in soup.find_all('a', href=True):
            text = link.get_text(strip=True)
            if 'instant' in text.lower() and 'dl' in text.lower():
                return link.get('href')
                
        logger.warning(f"No Instant DL link found for: {episode_url}")
        return None
    
    async def extract_google_drive_link_with_browser(self, instant_dl_url, max_wait_time=30):
        """
        Use headless browser to extract Google Drive link from JavaScript-generated content.
        This version is more robust against ad links.
        """
        logger.info(f"Using headless browser for: {instant_dl_url}")
        
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                )
                page = await context.new_page()

                # Block common ad domains to prevent them from loading/interfering
                def block_ads(route):
                    if any(ad_domain in route.request.url for ad_domain in self.ad_domains):
                        return route.abort()
                    return route.continue_()
                await page.route("**/*", block_ads)

                try:
                    logger.info(f"Navigating to page: {instant_dl_url}")
                    await page.goto(instant_dl_url, wait_until='domcontentloaded', timeout=60000)
                    
                    start_time = time.time()
                    while time.time() - start_time < max_wait_time:
                        # Look for all links on the page
                        links = await page.query_selector_all('a[href]')
                        
                        for link in links:
                            href = await link.get_attribute('href')
                            if href:
                                # **CRITICAL FIX**: Check if the link *starts with* a known Google Drive URL.
                                # This prevents matching ad links with Google URLs in their query parameters.
                                if href.startswith("https://video-downloads.googleusercontent.com") or \
                                   href.startswith("https://drive.google.com"):
                                    logger.info(f"Found valid Google Drive link: {href[:100]}...")
                                    return href
                        
                        # If not found, wait a bit for JS to execute
                        logger.info(f"Waiting for link... ({int(time.time() - start_time)}s / {max_wait_time}s)")
                        await page.wait_for_timeout(2000) # Wait 2 seconds before checking again

                    # Fallback: If the loop finishes, analyze the final page content with a strict regex
                    logger.warning("Primary link search failed. Analyzing final page content.")
                    content = await page.content()
                    
                    # Regex to find full Google Drive URLs
                    google_urls = re.findall(r'https?://(?:video-downloads\.googleusercontent\.com|drive\.google\.com)/[^"\'>\s]+', content)
                    
                    if google_urls:
                        for url in google_urls:
                            if 'google' in urlparse(url).netloc:
                                logger.info(f"Found Google Drive link via content analysis: {url[:100]}...")
                                return url

                    logger.error(f"No Google Drive link found on {instant_dl_url} after {max_wait_time} seconds.")
                    return None
                    
                except Exception as e:
                    logger.error(f"Browser operation failed for {instant_dl_url}: {str(e)}")
                    return None
                    
                finally:
                    await context.close()
                    await browser.close()
                    
        except Exception as e:
            logger.error(f"Failed to launch browser: {str(e)}")
            return None
    
    async def process_main_url(self, main_url):
        """Process the main URL and extract all episode download links"""
        logger.info(f"Processing main URL: {main_url}")
        
        # Fetch main page
        html_content = await self.fetch_page(main_url)
        if not html_content:
            return {"error": "Failed to fetch main page"}
        
        # Extract episode links
        episode_links = self.extract_episode_links(html_content, main_url)
        if not episode_links:
            return {"error": "No episode links found"}
        
        results = []
        
        for i, episode in enumerate(episode_links, 1):
            episode_name = episode['episode_name']
            episode_url = episode['episode_url']
            
            logger.info(f"Processing {episode_name} ({i}/{len(episode_links)}): {episode_url}")
            
            try:
                # Get Instant DL link
                instant_dl_url = await self.extract_instant_dl_link(episode_url)
                if not instant_dl_url:
                    results.append({
                        'episode': episode_name,
                        'status': 'failed',
                        'error': 'Instant DL link not found'
                    })
                    continue
                
                logger.info(f"Found instant DL URL: {instant_dl_url}")
                
                # Small delay between episodes to be respectful
                await asyncio.sleep(1)
                
                # Use headless browser to get final download link
                final_download_link = await self.extract_google_drive_link_with_browser(instant_dl_url)
                
                if not final_download_link:
                    logger.warning(f"Could not find final download link for {episode_name}")
                    results.append({
                        'episode': episode_name,
                        'episode_url': episode_url,
                        'instant_dl_url': instant_dl_url,
                        'status': 'partial',
                        'error': 'Final download link not found with headless browser'
                    })
                    continue
                
                results.append({
                    'episode': episode_name,
                    'episode_url': episode_url,
                    'instant_dl_url': instant_dl_url,
                    'final_download_link': final_download_link,
                    'status': 'success',
                    'timestamp': datetime.now().isoformat()
                })
                
                logger.info(f"Successfully processed {episode_name}")
                
                # Longer delay between successful extractions to be respectful
                await asyncio.sleep(3)
                
            except Exception as e:
                logger.error(f"Error processing {episode_name}: {str(e)}")
                results.append({
                    'episode': episode_name,
                    'status': 'failed',
                    'error': str(e)
                })
        
        # Save results
        await self.save_results(main_url, results)
        return results
    
    async def save_results(self, main_url, results):
        """Save results to JSON file"""
        data = {
            'main_url': main_url,
            'processed_at': datetime.now().isoformat(),
            'total_episodes': len(results),
            'successful': len([r for r in results if r['status'] == 'success']),
            'failed': len([r for r in results if r['status'] == 'failed']),
            'partial': len([r for r in results if r['status'] == 'partial']),
            'episodes': results
        }
        
        # Load existing data
        existing_data = []
        if os.path.exists(self.results_file):
            try:
                async with aiofiles.open(self.results_file, 'r') as f:
                    content = await f.read()
                    if content:
                        existing_data = json.loads(content)
            except (json.JSONDecodeError, FileNotFoundError):
                existing_data = []
        
        # Add new data
        existing_data.append(data)
        
        # Save updated data
        async with aiofiles.open(self.results_file, 'w') as f:
            await f.write(json.dumps(existing_data, indent=2))
        
        logger.info(f"Results saved to {self.results_file}")

# Web Server
class WebServer:
    def __init__(self):
        self.app = web.Application()
        self.setup_routes()
        
    def setup_routes(self):
        self.app.router.add_get('/', self.dashboard)
        self.app.router.add_post('/process', self.process_url)
        self.app.router.add_get('/results', self.get_results)
        self.app.router.add_get('/download/{filename}', self.download_file)
    
    async def dashboard(self, request):
        """Serve the dashboard HTML with copy functionality"""
        html = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Episode Link Extractor - Headless Browser Version</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; color: #333; }
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
        .header { text-align: center; color: white; margin-bottom: 30px; }
        .header h1 { font-size: 2.5rem; margin-bottom: 10px; text-shadow: 0 2px 4px rgba(0,0,0,0.3); }
        .card { background: white; border-radius: 15px; padding: 30px; box-shadow: 0 10px 30px rgba(0,0,0,0.2); margin-bottom: 20px; }
        .form-group { margin-bottom: 20px; }
        label { display: block; margin-bottom: 8px; font-weight: 600; color: #555; }
        input[type="url"] { width: 100%; padding: 12px 15px; border: 2px solid #e0e0e0; border-radius: 8px; font-size: 16px; transition: border-color 0.3s ease; }
        input[type="url"]:focus { outline: none; border-color: #667eea; }
        .btn { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 12px 30px; border: none; border-radius: 8px; font-size: 16px; cursor: pointer; transition: transform 0.2s ease, background-color 0.2s; }
        .btn:hover { transform: translateY(-2px); }
        .btn:disabled { opacity: 0.6; cursor: not-allowed; transform: none; }
        .status { margin-top: 20px; padding: 15px; border-radius: 8px; display: none; }
        .status.success { background: #d4edda; border: 1px solid #c3e6cb; color: #155724; }
        .status.error { background: #f8d7da; border: 1px solid #f5c6cb; color: #721c24; }
        .status.processing { background: #d1ecf1; border: 1px solid #bee5eb; color: #0c5460; }
        .results { margin-top: 30px; }
        .episode-item { display: flex; align-items: center; justify-content: space-between; background: #f8f9fa; padding: 15px; margin-bottom: 10px; border-radius: 8px; border-left: 4px solid #667eea; }
        .episode-item.partial { border-left-color: #ffc107; }
        .episode-item.failed { border-left-color: #dc3545; }
        .episode-details { flex-grow: 1; }
        .episode-name { font-weight: 600; margin-bottom: 5px; }
        .download-link { word-break: break-all; color: #667eea; text-decoration: none; font-size: 14px; }
        .download-link:hover { text-decoration: underline; }
        .stats { display: flex; justify-content: space-around; margin-bottom: 20px; }
        .stat { text-align: center; }
        .stat-number { font-size: 2rem; font-weight: bold; color: #667eea; }
        .stat-label { color: #666; font-size: 14px; }
        .loading { display: inline-block; width: 20px; height: 20px; border: 3px solid #f3f3f3; border-top: 3px solid #667eea; border-radius: 50%; animation: spin 1s linear infinite; margin-right: 10px; vertical-align: middle; }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
        .info-box { background: #e9ecef; padding: 15px; border-radius: 8px; margin-bottom: 20px; border-left: 4px solid #17a2b8; }
        .info-box h4 { color: #17a2b8; margin-bottom: 8px; }
        .copy-btn { background: #e9ecef; border: 1px solid #ced4da; border-radius: 5px; padding: 4px 10px; font-size: 12px; cursor: pointer; margin-left: 10px; transition: background-color 0.2s; white-space: nowrap; }
        .copy-btn:hover { background-color: #dee2e6; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🎬 Episode Link Extractor - Headless Browser</h1>
            <p>Extracts Google Drive links from fxlinks.fun using headless browser automation</p>
        </div>
        
        <div class="card">
            <div class="info-box">
                <h4>🚀 Headless Browser Features</h4>
                <p>This version uses Playwright headless browser to execute JavaScript, wait for dynamic content, and extract the final Google Drive links, avoiding ads and redirects.</p>
                <p><strong>Note:</strong> You need to install Playwright: <code>pip install playwright && playwright install chromium</code></p>
            </div>
            
            <h2>Process New URL</h2>
            <form id="urlForm">
                <div class="form-group">
                    <label for="url">Enter fxlinks.fun URL:</label>
                    <input type="url" id="url" name="url" placeholder="https://fxlinks.fun/elinks/dex60204" required>
                </div>
                <button type="submit" class="btn" id="processBtn">
                    Process Episodes
                </button>
            </form>
            
            <div id="status" class="status"></div>
        </div>
        
        <div class="card">
            <h2>Recent Results</h2>
            <div id="actionsContainer" style="margin-bottom: 15px;"></div>
            <div id="resultsContainer">
                <p>No results yet. Process a URL to see results here.</p>
            </div>
        </div>
    </div>

    <script>
        document.getElementById('urlForm').addEventListener('submit', async function(e) {
            e.preventDefault();
            
            const url = document.getElementById('url').value;
            const processBtn = document.getElementById('processBtn');
            const status = document.getElementById('status');
            
            processBtn.disabled = true;
            processBtn.innerHTML = '<span class="loading"></span>Processing...';
            status.className = 'status processing';
            status.style.display = 'block';
            status.textContent = 'Processing with headless browser - this may take some time...';
            
            try {
                const response = await fetch('/process', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({url: url})
                });
                
                const result = await response.json();
                
                if (result.error) {
                    status.className = 'status error';
                    status.textContent = 'Error: ' + result.error;
                } else {
                    const successCount = result.filter(r => r.status === 'success').length;
                    status.className = 'status success';
                    status.textContent = `Processing complete! Found ${successCount} successful links out of ${result.length} episodes.`;
                    loadResults();
                }
            } catch (error) {
                status.className = 'status error';
                status.textContent = 'An unexpected error occurred: ' + error.message;
            } finally {
                processBtn.disabled = false;
                processBtn.innerHTML = 'Process Episodes';
            }
        });
        
        async function loadResults() {
            try {
                const response = await fetch('/results');
                const data = await response.json();
                
                const resultsContainer = document.getElementById('resultsContainer');
                const actionsContainer = document.getElementById('actionsContainer');
                
                resultsContainer.innerHTML = '';
                actionsContainer.innerHTML = '';

                if (!data.length) {
                    resultsContainer.innerHTML = '<p>No results found.</p>';
                    return;
                }
                
                const latest = data[data.length - 1];
                
                const successfulLinks = latest.episodes
                    .filter(ep => ep.status === 'success' && ep.final_download_link)
                    .map(ep => ep.final_download_link);

                if (successfulLinks.length > 0) {
                    const copyBtn = document.createElement('button');
                    copyBtn.textContent = `📋 Copy All ${successfulLinks.length} Links`;
                    copyBtn.className = 'btn';
                    copyBtn.onclick = () => {
                        const linksText = successfulLinks.join('\\n');
                        navigator.clipboard.writeText(linksText).then(() => {
                            copyBtn.textContent = '✅ Copied!';
                            setTimeout(() => {
                                copyBtn.textContent = `📋 Copy All ${successfulLinks.length} Links`;
                            }, 2000);
                        }).catch(err => {
                            console.error('Failed to copy links: ', err);
                            alert('Failed to copy links.');
                        });
                    };
                    actionsContainer.appendChild(copyBtn);
                }
                
                let html = `
                    <div class="stats">
                        <div class="stat"><div class="stat-number">${latest.total_episodes}</div><div class="stat-label">Total</div></div>
                        <div class="stat"><div class="stat-number">${latest.successful}</div><div class="stat-label">Successful</div></div>
                        <div class="stat"><div class="stat-number">${latest.partial || 0}</div><div class="stat-label">Partial</div></div>
                        <div class="stat"><div class="stat-number">${latest.failed}</div><div class="stat-label">Failed</div></div>
                    </div>
                    <h3>Latest: ${latest.main_url}</h3>
                    <p><strong>Processed:</strong> ${new Date(latest.processed_at).toLocaleString()}</p>
                    <hr style="margin: 20px 0;">
                `;
                
                latest.episodes.forEach(episode => {
                    if (episode.status === 'success') {
                        html += `
                            <div class="episode-item">
                                <div class="episode-details">
                                    <div class="episode-name">✅ ${episode.episode}</div>
                                    <a href="${episode.final_download_link}" target="_blank" class="download-link">
                                        📁 ${episode.final_download_link.substring(0, 80)}...
                                    </a>
                                </div>
                                <button class="copy-btn" data-link="${episode.final_download_link}">Copy</button>
                            </div>
                        `;
                    } else if (episode.status === 'partial') {
                        html += `
                            <div class="episode-item partial">
                                <div class="episode-details">
                                    <div class="episode-name">⚠️ ${episode.episode}</div>
                                    <span style="color: #856404; font-size: 14px;">${episode.error}</span>
                                </div>
                            </div>
                        `;
                    } else {
                        html += `
                            <div class="episode-item failed">
                                <div class="episode-details">
                                    <div class="episode-name">❌ ${episode.episode}</div>
                                    <span style="color: #dc3545; font-size: 14px;">${episode.error}</span>
                                </div>
                            </div>
                        `;
                    }
                });
                
                resultsContainer.innerHTML = html;

                document.querySelectorAll('.copy-btn').forEach(button => {
                    button.addEventListener('click', () => {
                        const link = button.dataset.link;
                        navigator.clipboard.writeText(link).then(() => {
                            const originalText = button.textContent;
                            button.textContent = 'Copied!';
                            setTimeout(() => { button.textContent = originalText; }, 1500);
                        });
                    });
                });
                
            } catch (error) {
                console.error('Error loading results:', error);
                document.getElementById('resultsContainer').innerHTML = '<p class="status error">Could not load results.</p>';
            }
        }
        
        loadResults();
    </script>
</body>
</html>
        '''
        return web.Response(text=html, content_type='text/html')
    
    async def process_url(self, request):
        """Process a URL and extract episode links"""
        try:
            data = await request.json()
            url = data.get('url')
            
            if not url:
                return web.json_response({'error': 'URL is required'})
            
            async with EpisodeLinkExtractor() as extractor:
                results = await extractor.process_main_url(url)
                return web.json_response(results)
                
        except Exception as e:
            logger.error(f"Error processing URL: {str(e)}")
            return web.json_response({'error': str(e)})
    
    async def get_results(self, request):
        """Get stored results"""
        try:
            if os.path.exists('extracted_links.json'):
                async with aiofiles.open('extracted_links.json', 'r') as f:
                    content = await f.read()
                    data = json.loads(content)
                    return web.json_response(data)
            else:
                return web.json_response([])
        except Exception as e:
            logger.error(f"Error reading results: {str(e)}")
            return web.json_response([])
    
    async def download_file(self, request):
        """Serve a file from the current working directory safely."""
        try:
            filename = request.match_info.get('filename')
            if not filename:
                return web.Response(status=400, text='Filename is required')

            # Prevent path traversal
            safe_name = os.path.basename(filename)
            file_path = os.path.join(os.getcwd(), safe_name)

            if not os.path.exists(file_path) or not os.path.isfile(file_path):
                return web.Response(status=404, text='File not found')

            return web.FileResponse(path=file_path)
        except Exception as e:
            logger.error(f"Error serving file: {str(e)}")
            return web.Response(status=500, text='Internal Server Error')

def main():
    """Main function to run the server"""
    server = WebServer()
    
    print("🎬 Episode Link Extractor Server - Headless Browser Version")
    print("=" * 70)
    print("Server starting on http://localhost:8080")
    print("Improvements:")
    print("- Robust Google Drive link detection to avoid ads")
    print("- 'Copy All Links' button on dashboard (one link per line)")
    print("- Individual 'Copy' button for each link")
    print("- Basic ad-blocking for faster, cleaner extraction")
    print("=" * 70)
    print("Prerequisites: pip install aiohttp beautifulsoup4 aiofiles playwright")
    print("Then run:      playwright install chromium")
    print("Open your browser and navigate to http://localhost:8080")
    print("=" * 70)
    
    web.run_app(server.app, host='localhost', port=8080)

if __name__ == '__main__':
    main()
