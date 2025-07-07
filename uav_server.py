#!/usr/bin/env python3
"""
UAV WebSocket Server
Havada olan UAV için optimize edilmiş WebSocket sunucusu
"""

import asyncio
import websockets
import json
import subprocess
import logging
import time
import psutil
import socket
from datetime import datetime
from typing import Dict, Set
import signal
import sys

# Logging yapılandırması
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/var/log/uav_server.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class UAVServer:
    def __init__(self):
        self.clients: Set[websockets.WebSocketServerProtocol] = set()
        self.is_running = True
        self.telemetry_interval = 1.0  # Telemetri gönderme aralığı (saniye)
        
        # İzin verilen komutlar (güvenlik için)
        self.allowed_commands = {
            'system_info': 'uname -a',
            'disk_usage': 'df -h',
            'memory_info': 'free -h',
            'network_info': 'ip addr show',
            'processes': 'ps aux',
            'uptime': 'uptime',
            'temperature': 'sensors',
            'gpio_status': 'gpio readall',  # Raspberry Pi için
            'wifi_status': 'iwconfig',
            'ping_test': 'ping -c 4 8.8.8.8',
            'reboot': 'sudo reboot',
            'shutdown': 'sudo shutdown -h now'
        }
        
        # Signal handler'ları ayarla
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
    
    def signal_handler(self, signum, frame):
        """Graceful shutdown için signal handler"""
        logger.info(f"Signal {signum} alındı, sunucu kapatılıyor...")
        self.is_running = False
        sys.exit(0)
    
    async def register_client(self, websocket):
        """Yeni istemci kaydı"""
        self.clients.add(websocket)
        client_ip = websocket.remote_address[0]
        logger.info(f"Yeni istemci bağlandı: {client_ip}")
        
        # Bağlantı onayı gönder
        await self.send_to_client(websocket, {
            'type': 'connection_established',
            'message': 'UAV bağlantısı başarılı',
            'server_time': datetime.now().isoformat(),
            'server_info': await self.get_system_info()
        })
    
    async def unregister_client(self, websocket):
        """İstemci kaydını kaldır"""
        self.clients.discard(websocket)
        logger.info(f"İstemci bağlantısı kesildi: {websocket.remote_address[0]}")
    
    async def send_to_client(self, websocket, data):
        """Tek istemciye mesaj gönder"""
        try:
            await websocket.send(json.dumps(data))
        except websockets.exceptions.ConnectionClosed:
            await self.unregister_client(websocket)
    
    async def broadcast_to_all(self, data):
        """Tüm istemcilere mesaj gönder"""
        if self.clients:
            await asyncio.gather(
                *[self.send_to_client(client, data) for client in self.clients.copy()],
                return_exceptions=True
            )
    
    async def get_system_info(self):
        """Sistem bilgilerini al"""
        return {
            'hostname': socket.gethostname(),
            'cpu_percent': psutil.cpu_percent(),
            'memory_percent': psutil.virtual_memory().percent,
            'disk_percent': psutil.disk_usage('/').percent,
            'uptime': time.time() - psutil.boot_time(),
            'network_interfaces': self.get_network_interfaces()
        }
    
    def get_network_interfaces(self):
        """Ağ arayüzlerini al"""
        interfaces = {}
        for interface, addresses in psutil.net_if_addrs().items():
            for addr in addresses:
                if addr.family == socket.AF_INET:
                    interfaces[interface] = addr.address
        return interfaces
    
    async def execute_command(self, command_key, params=None):
        """Güvenli komut çalıştırma"""
        if command_key not in self.allowed_commands:
            return {
                'success': False,
                'error': f'Komut izin verilmiyor: {command_key}',
                'available_commands': list(self.allowed_commands.keys())
            }
        
        try:
            command = self.allowed_commands[command_key]
            
            # Parametreli komutlar için
            if params and command_key == 'ping_test':
                target = params.get('target', '8.8.8.8')
                command = f'ping -c 4 {target}'
            
            logger.info(f"Komut çalıştırılıyor: {command}")
            
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30  # 30 saniye timeout
            )
            
            return {
                'success': True,
                'command': command_key,
                'output': result.stdout,
                'error': result.stderr,
                'return_code': result.returncode,
                'timestamp': datetime.now().isoformat()
            }
            
        except subprocess.TimeoutExpired:
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
    
    async def handle_message(self, websocket, message):
        """Gelen mesajları işle"""
        try:
            data = json.loads(message)
            message_type = data.get('type')
            
            if message_type == 'command':
                command_key = data.get('command')
                params = data.get('params', {})
                
                result = await self.execute_command(command_key, params)
                await self.send_to_client(websocket, {
                    'type': 'command_result',
                    'request_id': data.get('request_id'),
                    **result
                })
            
            elif message_type == 'ping':
                await self.send_to_client(websocket, {
                    'type': 'pong',
                    'timestamp': datetime.now().isoformat()
                })
            
            elif message_type == 'get_telemetry':
                telemetry = await self.get_telemetry_data()
                await self.send_to_client(websocket, {
                    'type': 'telemetry',
                    **telemetry
                })
            
            else:
                await self.send_to_client(websocket, {
                    'type': 'error',
                    'message': f'Bilinmeyen mesaj tipi: {message_type}'
                })
                
        except json.JSONDecodeError:
            await self.send_to_client(websocket, {
                'type': 'error',
                'message': 'Geçersiz JSON formatı'
            })
        except Exception as e:
            logger.error(f"Mesaj işleme hatası: {e}")
            await self.send_to_client(websocket, {
                'type': 'error',
                'message': str(e)
            })
    
    async def get_telemetry_data(self):
        """Telemetri verilerini al"""
        return {
            'timestamp': datetime.now().isoformat(),
            'system_info': await self.get_system_info(),
            'network_stats': self.get_network_stats(),
            'battery_info': self.get_battery_info() if hasattr(psutil, 'sensors_battery') else None
        }
    
    def get_network_stats(self):
        """Ağ istatistiklerini al"""
        stats = psutil.net_io_counters()
        return {
            'bytes_sent': stats.bytes_sent,
            'bytes_recv': stats.bytes_recv,
            'packets_sent': stats.packets_sent,
            'packets_recv': stats.packets_recv
        }
    
    def get_battery_info(self):
        """Batarya bilgilerini al (varsa)"""
        try:
            battery = psutil.sensors_battery()
            if battery:
                return {
                    'percent': battery.percent,
                    'power_plugged': battery.power_plugged,
                    'time_left': battery.secsleft if battery.secsleft != psutil.POWER_TIME_UNLIMITED else None
                }
        except:
            pass
        return None
    
    async def telemetry_broadcaster(self):
        """Periyodik telemetri yayını"""
        while self.is_running:
            try:
                if self.clients:
                    telemetry = await self.get_telemetry_data()
                    await self.broadcast_to_all({
                        'type': 'telemetry_broadcast',
                        **telemetry
                    })
                
                await asyncio.sleep(self.telemetry_interval)
            except Exception as e:
                logger.error(f"Telemetri yayın hatası: {e}")
                await asyncio.sleep(self.telemetry_interval)
    
    async def handle_client(self, websocket, path):
        """İstemci bağlantısını yönet"""
        await self.register_client(websocket)
        
        try:
            async for message in websocket:
                await self.handle_message(websocket, message)
        except websockets.exceptions.ConnectionClosed:
            logger.info("İstemci bağlantısı normal şekilde kapandı")
        except Exception as e:
            logger.error(f"İstemci işleme hatası: {e}")
        finally:
            await self.unregister_client(websocket)
    
    async def start_server(self, host='0.0.0.0', port=8765):
        """Sunucuyu başlat"""
        logger.info(f"UAV WebSocket sunucusu başlatılıyor: {host}:{port}")
        
        # Telemetri yayıncısını başlat
        telemetry_task = asyncio.create_task(self.telemetry_broadcaster())
        
        # WebSocket sunucusunu başlat
        server = await websockets.serve(
            self.handle_client,
            host,
            port,
            ping_interval=10,  # 10 saniyede bir ping
            ping_timeout=5,    # 5 saniye ping timeout
            max_size=1024*1024  # 1MB max mesaj boyutu
        )
        
        logger.info("UAV WebSocket sunucusu başarıyla başlatıldı")
        
        try:
            await server.wait_closed()
        except KeyboardInterrupt:
            logger.info("Sunucu kapatılıyor...")
        finally:
            telemetry_task.cancel()
            server.close()
            await server.wait_closed()

# Ana çalıştırma
if __name__ == "__main__":
    uav_server = UAVServer()
    
    # Komut satırı argümanları
    import argparse
    parser = argparse.ArgumentParser(description='UAV WebSocket Server')
    parser.add_argument('--host', default='0.0.0.0', help='Sunucu host adresi')
    parser.add_argument('--port', type=int, default=8765, help='Sunucu port numarası')
    parser.add_argument('--telemetry-interval', type=float, default=1.0, help='Telemetri gönderim aralığı (saniye)')
    
    args = parser.parse_args()
    
    uav_server.telemetry_interval = args.telemetry_interval
    
    try:
        asyncio.run(uav_server.start_server(args.host, args.port))
    except KeyboardInterrupt:
        logger.info("Sunucu durduruldu")
    except Exception as e:
        logger.error(f"Sunucu hatası: {e}")
        sys.exit(1)