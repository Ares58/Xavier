#!/usr/bin/env python3
"""
UAV Basit WebSocket Server
Python 3.6+ ile uyumlu, basit ve güvenilir UAV kontrol sunucusu
"""

import asyncio
import websockets
import json
import subprocess
import logging
import time
import threading
import signal
import sys
import os

# Logging yapılandırması
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class SimpleUAVServer:
    def __init__(self, host='0.0.0.0', port=8765):
        self.host = host
        self.port = port
        self.clients = set()
        self.running = True
        
        # İzin verilen komutlar
        self.commands = {
            'system_info': 'uname -a',
            'memory_info': 'free -h',
            'disk_usage': 'df -h',
            'network_info': 'ip addr show',
            'wifi_status': 'iwconfig 2>/dev/null || echo "iwconfig bulunamadı"',
            'ping_test': 'ping -c 4 8.8.8.8',
            'uptime': 'uptime',
            'date': 'date',
            'processes': 'ps aux | head -20',
            'temperature': 'sensors 2>/dev/null || echo "sensors bulunamadı"'
        }
        
        # Signal handler
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
    
    def signal_handler(self, signum, frame):
        logger.info(f"Signal {signum} alındı, sunucu kapatılıyor...")
        self.running = False
        sys.exit(0)
    
    def get_system_info(self):
        """Basit sistem bilgilerini al"""
        try:
            # CPU bilgisi
            with open('/proc/loadavg', 'r') as f:
                load_avg = f.read().strip().split()[:3]
            
            # Memory bilgisi
            with open('/proc/meminfo', 'r') as f:
                meminfo = f.read()
                mem_total = None
                mem_free = None
                for line in meminfo.split('\n'):
                    if line.startswith('MemTotal:'):
                        mem_total = int(line.split()[1])
                    elif line.startswith('MemAvailable:'):
                        mem_free = int(line.split()[1])
                        break
            
            # Disk bilgisi
            disk_usage = os.statvfs('/')
            disk_free = disk_usage.f_bavail * disk_usage.f_frsize
            disk_total = disk_usage.f_blocks * disk_usage.f_frsize
            
            return {
                'load_average': load_avg,
                'memory_total_kb': mem_total,
                'memory_free_kb': mem_free,
                'memory_usage_percent': ((mem_total - mem_free) / mem_total * 100) if mem_total and mem_free else 0,
                'disk_free_bytes': disk_free,
                'disk_total_bytes': disk_total,
                'disk_usage_percent': ((disk_total - disk_free) / disk_total * 100) if disk_total > 0 else 0,
                'uptime': time.time() - os.path.getmtime('/proc/uptime'),
                'timestamp': time.time()
            }
        except Exception as e:
            logger.error(f"Sistem bilgisi alınamadı: {e}")
            return {'error': str(e)}
    
    def execute_command(self, command_key, params=None):
        """Komut çalıştır"""
        if command_key not in self.commands:
            return {
                'success': False,
                'error': f'Komut bulunamadı: {command_key}',
                'available_commands': list(self.commands.keys())
            }
        
        try:
            command = self.commands[command_key]
            
            # Ping testi için özel parametre
            if command_key == 'ping_test' and params and 'target' in params:
                target = params['target']
                # Basit IP/domain validasyonu
                if target.replace('.', '').replace('-', '').isalnum() or '.' in target:
                    command = f'ping -c 4 {target}'
                else:
                    return {'success': False, 'error': 'Geçersiz hedef adresi'}
            
            logger.info(f"Komut çalıştırılıyor: {command}")
            
            # Komut çalıştır
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            # Timeout ile bekle
            stdout, stderr = process.communicate(timeout=30)
            
            return {
                'success': True,
                'command': command_key,
                'output': stdout,
                'error': stderr,
                'return_code': process.returncode,
                'timestamp': time.time()
            }
            
        except subprocess.TimeoutExpired:
            process.kill()
            return {
                'success': False,
                'error': 'Komut zaman aşımına uğradı (30s)',
                'command': command_key
            }
        except Exception as e:
            logger.error(f"Komut çalıştırma hatası: {e}")
            return {
                'success': False,
                'error': str(e),
                'command': command_key
            }
    
    async def handle_client(self, websocket, path):
        """İstemci ile iletişim"""
        client_ip = websocket.remote_address[0]
        logger.info(f"Yeni istemci: {client_ip}")
        
        # İstemciyi kaydet
        self.clients.add(websocket)
        
        try:
            # Bağlantı onayı gönder
            await websocket.send(json.dumps({
                'type': 'connection_established',
                'message': 'UAV bağlantısı başarılı',
                'timestamp': time.time(),
                'system_info': self.get_system_info()
            }))
            
            # Mesaj döngüsü
            async for message in websocket:
                try:
                    data = json.loads(message)
                    await self.process_message(websocket, data)
                except json.JSONDecodeError:
                    await websocket.send(json.dumps({
                        'type': 'error',
                        'message': 'Geçersiz JSON'
                    }))
                except Exception as e:
                    logger.error(f"Mesaj işleme hatası: {e}")
                    await websocket.send(json.dumps({
                        'type': 'error',
                        'message': str(e)
                    }))
        
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"İstemci ayrıldı: {client_ip}")
        except Exception as e:
            logger.error(f"İstemci hatası: {e}")
        finally:
            self.clients.discard(websocket)
    
    async def process_message(self, websocket, data):
        """Mesaj işle"""
        message_type = data.get('type')
        
        if message_type == 'command':
            command_key = data.get('command')
            params = data.get('params', {})
            request_id = data.get('request_id')
            
            result = self.execute_command(command_key, params)
            result['type'] = 'command_result'
            result['request_id'] = request_id
            
            await websocket.send(json.dumps(result))
        
        elif message_type == 'ping':
            await websocket.send(json.dumps({
                'type': 'pong',
                'timestamp': time.time()
            }))
        
        elif message_type == 'get_system_info':
            system_info = self.get_system_info()
            await websocket.send(json.dumps({
                'type': 'system_info',
                'data': system_info
            }))
        
        elif message_type == 'get_telemetry':
            telemetry = {
                'type': 'telemetry',
                'timestamp': time.time(),
                'system_info': self.get_system_info()
            }
            await websocket.send(json.dumps(telemetry))
        
        else:
            await websocket.send(json.dumps({
                'type': 'error',
                'message': f'Bilinmeyen mesaj tipi: {message_type}'
            }))
    
    async def telemetry_broadcaster(self):
        """Telemetri yayınlayıcısı"""
        while self.running:
            try:
                if self.clients:
                    telemetry = {
                        'type': 'telemetry_broadcast',
                        'timestamp': time.time(),
                        'system_info': self.get_system_info()
                    }
                    
                    # Tüm istemcilere gönder
                    disconnected = []
                    for client in self.clients.copy():
                        try:
                            await client.send(json.dumps(telemetry))
                        except websockets.exceptions.ConnectionClosed:
                            disconnected.append(client)
                        except Exception as e:
                            logger.error(f"Telemetri gönderme hatası: {e}")
                            disconnected.append(client)
                    
                    # Bağlantısı kopan istemcileri temizle
                    for client in disconnected:
                        self.clients.discard(client)
                
                await asyncio.sleep(2)  # 2 saniyede bir telemetri
                
            except Exception as e:
                logger.error(f"Telemetri yayın hatası: {e}")
                await asyncio.sleep(2)
    
    def start_server(self):
        """Sunucuyu başlat"""
        logger.info(f"UAV WebSocket sunucusu başlatılıyor: {self.host}:{self.port}")
        
        # Event loop al
        loop = asyncio.get_event_loop()
        
        # Sunucu başlat
        start_server = websockets.serve(
            self.handle_client,
            self.host,
            self.port,
            ping_interval=20,
            ping_timeout=10
        )
        
        # Telemetri task'ı başlat
        telemetry_task = loop.create_task(self.telemetry_broadcaster())
        
        try:
            loop.run_until_complete(start_server)
            logger.info("Sunucu başarıyla başlatıldı")
            loop.run_forever()
        except KeyboardInterrupt:
            logger.info("Sunucu kapatılıyor...")
        except Exception as e:
            logger.error(f"Sunucu hatası: {e}")
        finally:
            telemetry_task.cancel()
            loop.close()

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='UAV Basit WebSocket Server')
    parser.add_argument('--host', default='0.0.0.0', help='Host adresi')
    parser.add_argument('--port', type=int, default=8765, help='Port numarası')
    
    args = parser.parse_args()
    
    server = SimpleUAVServer(args.host, args.port)
    server.start_server()