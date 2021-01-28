
import asyncio
import base64

from asysocks import logger
from asysocks.common.constants import SocksServerVersion, SocksCommsMode
from asysocks.protocol.http import HTTPResponse, HTTPProxyAuthRequiredException, HTTPProxyAuthFailed
from asysocks.protocol.socks4 import SOCKS4Request, SOCKS4Reply, SOCKS4CDCode
from asysocks.protocol.socks4a import SOCKS4ARequest, SOCKS4AReply, SOCKS4ACDCode
from asysocks.protocol.socks5 import SOCKS5Method, SOCKS5Nego, SOCKS5NegoReply, SOCKS5Request, SOCKS5Reply, SOCKS5ReplyType, SOCKS5PlainAuth, SOCKS5PlainAuthReply, SOCKS5ServerErrorReply, SOCKS5AuthFailed


class SocksTunnelError(Exception):
	def __init__(self, innerexception, message="Something failed setting up the tunnel! See innerexception for more details"):
		self.innerexception = innerexception
		self.message = message
		super().__init__(self.message)

class SOCKSClient:
	def __init__(self, comms, proxies, bind_evt = None, channel_open_evt = None):
		self.proxies = proxies
		if isinstance(proxies, list) is False:
			self.proxies = [proxies]
		self.comms = comms
		self.server_task = None
		self.proxytask_in = None
		self.proxytask_out = None
		self.proxy_stopped_evt = None
		self.__http_auth = None
		self.bind_progress_evt = bind_evt
		self.bind_port = None
		self.channel_open_evt = channel_open_evt
	
	async def terminate(self):
		if self.proxy_stopped_evt is not None:
			self.proxy_stopped_evt.set()
		if self.proxytask_in is not None:
			self.proxytask_in.cancel()
		if self.proxytask_out is not None:
			self.proxytask_out.cancel()


	@staticmethod
	async def proxy_stream(in_stream, out_stream, proxy_stopped_evt, buffer_size = 4096, timeout = None):
		try:
			while True:
				read_task = asyncio.wait_for(
					in_stream.read(buffer_size),
					timeout = timeout
				)

				finished_tasks, pending_tasks = await asyncio.wait([read_task, proxy_stopped_evt.wait()], return_when=asyncio.FIRST_COMPLETED)
				for task in finished_tasks:
					result = await task
					if result is True:
						try:
							for pt in pending_tasks:
								last_data =	await asyncio.wait_for(pt, timeout = 0.5) 
						except:
							pass
						else:
							if last_data != b'':
								out_stream.write(result)
								await out_stream.drain()
						logger.debug('other side disconnected!')
						return
					else:
						if result == b'':
							logger.debug('Stream channel broken!')
							return
						out_stream.write(result)
						await out_stream.drain()
						for pt in pending_tasks:
							pt.cancel()
		except asyncio.CancelledError:
			return
		except asyncio.TimeoutError:
			logger.debug('proxy_stream timeout! (timeout=%s)' % str(timeout))
		except Exception as e:
			logger.debug('proxy_stream err: %s' % str(e))
		finally:
			out_stream.close()
			proxy_stopped_evt.set()

	@staticmethod
	async def proxy_queue_in(in_queue, writer, proxy_stopped_evt, buffer_size = 4096):
		try:
			while not proxy_stopped_evt.is_set():
				data = await in_queue.get()
				if data is None:
					logger.debug('proxy_queue_in client disconncted!')
					return
				writer.write(data)
				await writer.drain()
		except asyncio.CancelledError:
			return
		except Exception as e:
			logger.debug('proxy_queue_in err: %s' % str(e))
		finally:
			proxy_stopped_evt.set()
			try:
				writer.close()
			except:
				pass

	@staticmethod
	async def proxy_queue_out(out_queue, reader, proxy_stopped_evt, buffer_size = 4096, timeout = None):
		try:
			while True:
				data = await asyncio.wait_for(
					reader.read(buffer_size),
					timeout = timeout
				)
				if data == b'':
					logger.debug('proxy_queue_out endpoint disconncted!')
					await out_queue.put((None, Exception('proxy_queue_out endpoint disconncted gracefully!')))
					return
				await out_queue.put((data, None))
		except asyncio.CancelledError:
			await out_queue.put((None, Exception('proxy_queue_out got cancelled!')))
			return
		except Exception as e:
			logger.debug('')
			try:
				await out_queue.put((None, e))
			except:
				pass
		finally:
			proxy_stopped_evt.set()
	

	async def run_http(self, proxy, remote_reader, remote_writer, timeout = None):		
		try:
			print('!!!!! __http_auth is not resolved !!!!')
			logger.debug('[HTTP] Opening channel to %s:%s' % (proxy.endpoint_ip, proxy.endpoint_port))
			connect_cmd = 'CONNECT %s:%s HTTP/1.1\r\nHost: %s:%s\r\n' % (proxy.endpoint_ip, proxy.endpoint_port, proxy.endpoint_ip, proxy.endpoint_port)
			connect_cmd = connect_cmd.encode()

			__http_auth = None
			if __http_auth is None:
				
				remote_writer.write(connect_cmd + b'\r\n')
				#remote_writer.write(connect_cmd + b'\r\n')
				await asyncio.wait_for(remote_writer.drain(), timeout = timeout)
				
				resp, err = await HTTPResponse.from_streamreader(remote_reader, timeout=timeout)
				if err is not None:
					raise err

				if resp.status == 200:
					logger.debug('[HTTP] Server succsessfully connected!')
					return True, None
			
				elif resp.status == 407:
					logger.debug('[HTTP] Server proxy auth required!')

					if proxy.credential is None:
						raise Exception('HTTP proxy auth required but no credential set!')
					
					auth_type = resp.headers_upper.get('PROXY-AUTHENTICATE', None)
					if auth_type is None:
						raise Exception('HTTP proxy requires authentication, but requested auth type could not be determined')
					
					auth_type, _ = auth_type.split(' ', 1)
					__http_auth = auth_type.upper()

					return False, HTTPProxyAuthRequiredException()
			
			elif __http_auth == 'BASIC':
				auth_data = base64.b64encode(('%s:%s' % (proxy.credential.username, proxy.credential.password) ).encode() )
				auth_connect = connect_cmd + b'Proxy-Authorization: Basic ' + auth_data + b'\r\n'
				remote_writer.write(auth_connect + b'\r\n')
				await asyncio.wait_for(remote_writer.drain(), timeout = timeout)

				resp, err = await HTTPResponse.from_streamreader(remote_reader, timeout=timeout)
				if err is not None:
					raise err

				if resp.status == 200:
					logger.debug('[HTTP] Server proxy auth succsess!')
					return True, None
					
				else:
					raise HTTPProxyAuthFailed() #raise Exception('Proxy auth failed!')
				
			else:
				raise Exception('HTTP proxy requires %s authentication, but it\'s not implemented' % __http_auth)


		except Exception as e:
			logger.debug('run_http')
			return False, e

	async def run_socks4(self, proxy, remote_reader, remote_writer, timeout = None):
		"""
		Does the intial "handshake" instructing the remote server to set up the connection to the endpoint
		"""
		try:
			logger.debug('[SOCKS4 %s:%s] Opening channel to %s:%s (target)' % (proxy.server_ip, proxy.server_port, proxy.endpoint_ip, proxy.endpoint_port))
			req = SOCKS4Request.from_target(proxy)
			if proxy.credential is not None and proxy.credential.username is not None:
				req.USERID = proxy.credential.username
			remote_writer.write(req.to_bytes())
			await asyncio.wait_for(remote_writer.drain(), timeout = timeout)
			rep, err = await asyncio.wait_for(SOCKS4Reply.from_streamreader(remote_reader), timeout = timeout)
			if err is not None:
				raise err

			if rep is None:
				raise Exception('Socks server failed to reply to CONNECT request!')

			if rep.CD != SOCKS4CDCode.REP_GRANTED:
				raise Exception('Socks server returned error on CONNECT! %s' % rep.CD.value)
			logger.debug('[SOCKS4] Channel opened')
			return True, None
		except Exception as e:
			logger.debug('run_socks4')
			return False, e

	async def run_socks4a(self, proxy, remote_reader, remote_writer, timeout = None):
		"""
		Does the intial "handshake" instructing the remote server to set up the connection to the endpoint
		"""
		try:
			logger.debug('[SOCKS4A %s:%s] Opening channel to %s:%s (target)' % (proxy.server_ip, proxy.server_port, proxy.endpoint_ip, proxy.endpoint_port))
			req = SOCKS4ARequest.from_target(proxy)
			if proxy.credential is not None and proxy.credential.username is not None:
				req.USERID = proxy.credential.username
			remote_writer.write(req.to_bytes())
			await asyncio.wait_for(remote_writer.drain(), timeout = timeout)
			rep, err = await asyncio.wait_for(SOCKS4AReply.from_streamreader(remote_reader), timeout = timeout)
			if err is not None:
				raise err

			if rep is None:
				raise Exception('Socks server failed to reply to CONNECT request!')

			if rep.CD != SOCKS4ACDCode.REP_GRANTED:
				raise Exception('Socks server returned error on CONNECT! %s' % rep.CD.value)
			logger.debug('[SOCKS4A] Channel opened')
			return True, None
		except Exception as e:
			logger.debug('run_socks4a')
			return False, e

	async def run_socks5(self, proxy, remote_reader, remote_writer, timeout = None):
		"""
		Does the intial "handshake" instructing the remote server to set up the connection to the endpoint
		"""
		sname = proxy.get_sname()
		tname = proxy.get_tname()
		methods = [SOCKS5Method.NOAUTH]
		if proxy.credential is not None and proxy.credential.username is not None and proxy.credential.password is not None:
			methods.append(SOCKS5Method.PLAIN)
			#methods = [SOCKS5Method.PLAIN]
		try:
			nego = SOCKS5Nego.from_methods(methods)
			logger.debug('[SOCKS5 %s][SETUP] Sending negotiation command to server' % sname)
			remote_writer.write(nego.to_bytes())
			await asyncio.wait_for(
				remote_writer.drain(), 
				timeout = timeout
			)

			rep_nego = await asyncio.wait_for(
				SOCKS5NegoReply.from_streamreader(remote_reader), 
				timeout = timeout
			)
			logger.debug(
				'[SOCKS5 %s] Got negotiation reply! Server choosen auth type: %s' % (sname, rep_nego.METHOD.name)
			)
			
			if rep_nego.METHOD == SOCKS5Method.PLAIN:
				if proxy.credential is None or proxy.credential.username is None or proxy.credential.password is None:
					raise Exception('SOCKS5 %s] server requires PLAIN authentication, but no credentials were supplied!' % sname)
				
				logger.debug('[SOCKS5 %s]Preforming plaintext auth' % sname)
				remote_writer.write(
					SOCKS5PlainAuth.construct(
						proxy.credential.username, 
						proxy.credential.password
					).to_bytes()
				)
				await asyncio.wait_for(
					remote_writer.drain(), 
					timeout=None
				)
				rep_data = await asyncio.wait_for(
					remote_reader.read(2),
					timeout = timeout
				)

				if rep_data == b'':
					raise SOCKS5AuthFailed() #raise Exception('Plaintext auth failed! Bad username or password')

				rep_nego = SOCKS5PlainAuthReply.from_bytes(rep_data)

				if rep_nego.STATUS != SOCKS5ReplyType.SUCCEEDED:
					raise SOCKS5AuthFailed() #raise Exception('Plaintext auth failed! Bad username or password')

			elif rep_nego.METHOD == SOCKS5Method.GSSAPI:
				raise Exception('[SOCKS5 %s] server requires GSSAPI authentication, but it\'s not supported at the moment' % sname)

			logger.debug('[SOCKS5 %s] Opening channel to %s' % (sname, tname))
			
			if proxy.only_auth is True:
				return True, None

			remote_writer.write(
				SOCKS5Request.from_target(
					proxy
				).to_bytes()
			)
			await asyncio.wait_for(
				remote_writer.drain(), 
				timeout=timeout
			)

			rep = await asyncio.wait_for(
				SOCKS5Reply.from_streamreader(remote_reader), 
				timeout=timeout
			)
			if rep.REP != SOCKS5ReplyType.SUCCEEDED:
				logger.info('[SOCKS5 %s] remote end failed to connect to proxy! Reson: %s' % (sname, rep.REP.name))
				raise SOCKS5ServerErrorReply(rep.REP)
			
			if proxy.is_bind is False:
				logger.debug('[SOCKS5 %s] Successfully set up the connection to the endpoint %s ' % (sname,tname))
				return True, None
			
			logger.debug('[SOCKS5 %s] BIND first set completed, port %s available!' % (sname , rep.BIND_PORT))
			#bind in progress, waiting for a second reply to notify us that the remote endpoint connected back.
			
			self.bind_port = rep.BIND_PORT
			self.bind_progress_evt.set() #notifying that the bind port is now available on the socks server to be used
			if proxy.only_bind is True:
				return True, None

			rep = await asyncio.wait_for(
				SOCKS5Reply.from_streamreader(remote_reader), 
				timeout=timeout
			)

			if rep.REP != SOCKS5ReplyType.SUCCEEDED:
				logger.info('[SOCKS5 %s] remote end failed to connect to proxy! Reson: %s' % (sname, rep.REP.name))
				raise SOCKS5ServerErrorReply(rep.REP)

			return True, None

		except Exception as e:
			logger.debug('[SOCKS5 %s] Error in run_socks5 %s' % (sname, e))
			return False, e

	async def handle_client(self, reader, writer):
		remote_writer = None
		remote_reader = None

		try:
			logger.debug('[handle_client] Client connected!')

			if len(self.proxies) > 1:
				logger.debug('Start chaining...')

			for _ in range(3, 0 , -1): #this is for HTTP auth...
				if remote_writer is not None:
					remote_writer.close()
				
				try:
					if self.proxies[0].version != SocksServerVersion.WSNET:
						remote_reader, remote_writer = await asyncio.wait_for(
							asyncio.open_connection(
								self.proxies[0].server_ip, 
								self.proxies[0].server_port,
								ssl=self.proxies[0].ssl_ctx,
							),
							timeout = self.proxies[0].timeout
						)
						logger.debug('Connected to socks server!')
					else:
						from asysocks.network.wsnet import WSNETNetwork
						remote_reader, remote_writer = await WSNETNetwork.open_connection(self.proxies[0].server_ip, self.proxies[0].server_port)

				except:
					logger.debug('Failed to connect to SOCKS server!')
					raise
					
				try:
					for i, proxy in enumerate(self.proxies):
						if proxy.version in [SocksServerVersion.SOCKS4, SocksServerVersion.SOCKS4S]:
							_, err = await self.run_socks4(proxy, remote_reader, remote_writer)
							if err is not None:
								if len(self.proxies) > 1 and i != len(self.proxies)-1:
									raise SocksTunnelError(err)
								raise err
							continue

						elif proxy.version in [SocksServerVersion.SOCKS4A]:
							_, err = await self.run_socks4a(proxy, remote_reader, remote_writer)
							if err is not None:
								if len(self.proxies) > 1 and i != len(self.proxies)-1:
									raise SocksTunnelError(err)
								raise err
							continue

						elif proxy.version in [SocksServerVersion.SOCKS5, SocksServerVersion.SOCKS5S]:
							_, err = await self.run_socks5(proxy, remote_reader, remote_writer)
							if err is not None:
								if len(self.proxies) > 1 and i != len(self.proxies)-1:
									raise SocksTunnelError(err)
								raise err
							continue
							
						elif proxy.version in [SocksServerVersion.HTTP, SocksServerVersion.HTTPS]:
							_, err = await self.run_http(proxy, remote_reader, remote_writer)
							if err is not None:
								if len(self.proxies) > 1 and i != len(self.proxies)-1:
									raise SocksTunnelError(err)
								raise err
							continue

						elif proxy.version in [SocksServerVersion.WSNET]:
							if i != 0:
								raise Exception("WSNET only supported as the first proxy in chain!")
							continue

						else:
							raise Exception('Unknown SOCKS version!')

					else:
						# no need to do more iterations because of HTTP at this point
						break

				except HTTPProxyAuthRequiredException:
					continue
				except:
					raise
					
			logger.debug('[handle_client] Starting proxy...')
			self.channel_open_evt.set()
			self.proxy_stopped_evt = asyncio.Event()
			self.proxytask_in = asyncio.create_task(
				SOCKSClient.proxy_stream(
					reader, 
					remote_writer, 
					self.proxy_stopped_evt , 
					buffer_size = self.proxies[0].buffer_size,
					timeout = self.proxies[0].endpoint_timeout
				)
			)
			self.proxytask_out = asyncio.create_task(
				SOCKSClient.proxy_stream(
					remote_reader, 
					writer, 
					self.proxy_stopped_evt, 
					buffer_size = self.proxies[0].buffer_size,
					timeout = self.proxies[0].endpoint_timeout
				)
			)
			logger.debug('[handle_client] Proxy started!')
			await self.proxy_stopped_evt.wait()
			logger.debug('[handle_client] Proxy stopped!')
			self.proxytask_in.cancel()
			self.proxytask_out.cancel()
		
		except Exception as e:
			logger.exception('[handle_client]')

		finally:
			if remote_writer is not None:
				remote_writer.close()

	async def handle_queue(self):
		logger.debug('[queue] Connecting to socks server...')
		#connecting to socks server
		remote_writer = None
		remote_reader = None
		if self.bind_progress_evt is None:
			self.bind_progress_evt = asyncio.Event()

		try:
			if len(self.proxies) > 1:
				logger.debug('Start chaining...')

			for _ in range(3, 0 , -1):
				if remote_writer is not None:
					remote_writer.close()
				
				try:
					if self.proxies[0].version != SocksServerVersion.WSNET:
						remote_reader, remote_writer = await asyncio.wait_for(
							asyncio.open_connection(
								self.proxies[0].server_ip, 
								self.proxies[0].server_port
							),
							timeout = self.proxies[0].timeout
						)

					else:
						from asysocks.network.wsnet import WSNETNetwork
						remote_reader, remote_writer = await WSNETNetwork.open_connection(self.proxies[0].server_ip, self.proxies[0].server_port)
				
				except:
					logger.debug('Failed to connect to SOCKS server!')
					raise

				logger.debug('[queue] Connected to socks server!')

				try:
					for i, proxy in enumerate(self.proxies):
						if proxy.version in [SocksServerVersion.SOCKS4, SocksServerVersion.SOCKS4S]:
							_, err = await self.run_socks4(proxy, remote_reader, remote_writer)
							if err is not None:
								if len(self.proxies) > 1 and i != len(self.proxies)-1:
									raise SocksTunnelError(err)
								raise err
							continue

						elif proxy.version in [SocksServerVersion.SOCKS4A]:
							_, err = await self.run_socks4a(proxy, remote_reader, remote_writer)
							if err is not None:
								if len(self.proxies) > 1 and i != len(self.proxies)-1:
									raise SocksTunnelError(err)
								raise err
							continue

						elif proxy.version in [SocksServerVersion.SOCKS5, SocksServerVersion.SOCKS5S]:
							_, err = await self.run_socks5(proxy, remote_reader, remote_writer)
							if err is not None:
								if len(self.proxies) > 1 and i != len(self.proxies)-1:
									raise SocksTunnelError(err)
								raise err
							continue
							
						elif proxy.version in [SocksServerVersion.HTTP, SocksServerVersion.HTTPS]:
							_, err = await self.run_http(proxy, remote_reader, remote_writer)
							if err is not None:
								if len(self.proxies) > 1 and i != len(self.proxies)-1:
									raise SocksTunnelError(err)
								raise err
							continue
						
						elif proxy.version in [SocksServerVersion.WSNET]:
							if i != 0:
								raise Exception("WSNET only supported as the first proxy in chain!")
							continue

						else:
							raise Exception('Unknown SOCKS version!')
					else:
						break

				except HTTPProxyAuthRequiredException:
					continue
				except:
					raise
			
			if self.proxies[-1].only_bind is True:
				return True, None

			if self.proxies[-1].only_open is True or self.proxies[-1].only_auth is True:
				# for auth guessing and connection testing
				return True, None

			if self.channel_open_evt is not None:
				self.channel_open_evt.set()
			logger.debug('[queue] Starting proxy...')
			
			self.proxy_stopped_evt = asyncio.Event()
			self.proxytask_in = asyncio.create_task(
				SOCKSClient.proxy_queue_in(
					self.comms.in_queue, 
					remote_writer, 
					self.proxy_stopped_evt, 
					buffer_size = self.proxies[0].buffer_size
				)
			)
			self.proxytask_out = asyncio.create_task(
				SOCKSClient.proxy_queue_out(
					self.comms.out_queue, 
					remote_reader, 
					self.proxy_stopped_evt, 
					buffer_size = self.proxies[0].buffer_size, 
					timeout = self.proxies[0].endpoint_timeout
				)
			)
			logger.debug('[queue] Proxy started!')
			await self.proxy_stopped_evt.wait()
			logger.debug('[queue] Proxy stopped!')
			self.proxytask_in.cancel()
			self.proxytask_out.cancel()

			return True, None
		
		except Exception as e:
			await self.comms.in_queue.put((None,e))
			await self.comms.out_queue.put((None,e))
			return False, e
		
		finally:
			if remote_writer is not None:
				remote_writer.close()


	async def run(self):
		if isinstance(self.proxies, list) is False and (self.proxies.is_bind is True and self.bind_progress_evt is None):
			self.bind_progress_evt = asyncio.Event()
		
		if self.channel_open_evt is None:
			self.channel_open_evt = asyncio.Event()
		
		if self.comms.mode == SocksCommsMode.LISTENER:
			server = await asyncio.start_server(
				self.handle_client, 
				self.comms.listen_ip, 
				self.comms.listen_port,

			)
			logger.debug('[PROXY] Awaiting server task now...')
			await server.serve_forever()

		else:
			await self.handle_queue()
		