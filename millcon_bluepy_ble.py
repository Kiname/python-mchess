import logging
import threading
import queue
import time

import mill_prot
try:
    from bluepy.btle import Scanner, DefaultDelegate, Peripheral
    bluepy_ble_support = True
except:
    bluepy_ble_support = False


class Transport():
    def __init__(self, que):
        if bluepy_ble_support == False:
            self.init = False
            return
        self.blemutex = threading.Lock()
        self.wrque = queue.Queue()
        self.log = logging.getLogger("MillenniumBluePyBLE")
        self.que = que  # asyncio.Queue()
        self.init = True
        self.is_open = False
        self.mil = None
        self.rx = None
        self.log.debug("bluepy_ble init ok")

    def search_board(self):
        self.log.debug("bluepy_ble: searching for boards")

        class ScanDelegate(DefaultDelegate):
            def __init__(self, log):
                self.log = log
                DefaultDelegate.__init__(self)

            def handleDiscovery(self, dev, isNewDev, isNewData):
                if isNewDev:
                    self.log.debug("Discovered device {}".format(dev.addr))
                elif isNewData:
                    self.log.debug(
                        "Received new data from {}".format(dev.addr))

        scanner = Scanner().withDelegate(ScanDelegate(self.log))

        try:
            self.blemutex.acquire()
            devices = scanner.scan(10.0)
            self.blemutex.release()
        except Exception as e:
            self.log.error(
                "BLE scanning failed. You might need to excecute the scan with root rights: {}".format(e))
            return None

        for bledev in devices:
            self.log.debug("Device {} ({}), RSSI={} dB".format(
                bledev.addr, bledev.addrType, bledev.rssi))
            for (adtype, desc, value) in bledev.getScanData():
                self.log.debug("  {} ({}) = {}".format(desc, adtype, value))
                if desc == "Complete Local Name":
                    if "MILLENNIUM CHESS" in value:
                        self.log.info(
                            "Autodetected Millennium board at Bluetooth LE address: {}, signal strength (rssi): {}".format(
                                bledev.addr, bledev.rssi))
                        return bledev.addr
        return None

    def test_board(self, address):
        self.log.debug("Testing ble at {}".format(address))
        if self.open_mt(address) is True:
            self.is_open = True
            return "1.0"
        else:
            return None

    def open_mt(self, address):
        self.log.debug('Starting worker-thread for bluepy ble')
        self.worker_thread_active = True
        self.worker_threader = threading.Thread(
            target=self.worker_thread, args=(self.log, address, self.wrque, self.que))
        self.worker_threader.setDaemon(True)
        self.worker_threader.start()

        return True

    def write_mt(self, msg):
        self.log.debug('write-que-entry {}'.format(msg))
        self.wrque.put(msg)

    def get_name(self):
        return "millcon_bluepy_ble"

    def is_init(self):
        return self.init

    def worker_thread(self, log, address, wrque, que):
        class PeriDelegate(DefaultDelegate):
            def __init__(self, log, que):
                self.log = log
                self.log.debug("Init delegate for peri")
                self.chunks = ""
                DefaultDelegate.__init__(self)

            def handleNotification(self, cHandle, data):
                self.log.debug(
                    "BLE: Handle: {}, data: {}".format(cHandle, data))
                rcv = ""
                for b in data:
                    rcv += chr(b & 127)
                self.log.debug('BLE received [{}]'.format(rcv))
                self.chunks += rcv
                if self.chunks[0] not in mill_prot.millennium_protocol_replies:
                    self.log.warning(
                        "Illegal reply start '{}' received, discarding".format(self.chunks[0]))
                    while len(self.chunks) > 0 and self.chunks[0] not in mill_prot.millennium_protocol_replies:
                        self.chunks = self.chunks[1:]
                if len(self.chunks) > 0:
                    mlen = mill_prot.millennium_protocol_replies[self.chunks[0]]
                    if len(self.chunks) >= mlen:
                        valmsg = self.chunks[:mlen]
                        self.log.debug(
                            'bluepy_ble received complete msg: {}'.format(valmsg))
                        if mill_prot.check_block_crc(valmsg):
                            que.put(valmsg)
                        self.chunks = self.chunks[mlen:]

        rx = None
        tx = None
        log.debug("bluepy_ble open_mt {}".format(address))
        try:
            mil = Peripheral(address)
        except Exception as e:
            log.warning(
                'Failed to create ble peripheral at {}'.format(address))
            exit(-1)
        try:
            services = mil.getServices()
        except:
            log.error(
                'Failed to enumerate services for {}, {}'.format(address, e))
            exit(-1)
        for ser in services:
            log.debug('Service: {}'.format(ser))
            chrs = ser.getCharacteristics()
            for chri in chrs:
                if chri.uuid == "49535343-1e4d-4bd9-ba61-23c647249616":  # TX char, rx for us
                    rx = chri
                    rxh = chri.getHandle()
                    # Enable notification magic:
                    log.debug('Enabling notifications')
                    mil.writeCharacteristic(
                        rxh+1, (1).to_bytes(2, byteorder='little'))
                if chri.uuid == "49535343-8841-43f4-a8d4-ecbe34729bb3":  # RX char, tx for us
                    tx = chri
                    txh = chri.getHandle()
                if chri.supportsRead():
                    log.debug("  {} UUID={} {} -> {}".format(chri, chri.uuid,
                                                             chri.propertiesToString(), chri.read()))
                else:
                    log.debug("  {} UUID={}{}".format(
                        chri, chri.uuid, chri.propertiesToString()))

        try:
            log.debug('Installing peripheral delegate')
            delegate = PeriDelegate(log, que)
            delegate.que = que
            mil.withDelegate(delegate)
        except Exception as e:
            log.error(
                'Failed to install peripheral delegate! {}'.format(e))

        while self.worker_thread_active is True:
            if wrque.empty() is False:
                msg = wrque.get()
                gpar = 0
                for b in msg:
                    gpar = gpar ^ ord(b)
                msg = msg+mill_prot.hex2(gpar)
                log.debug("blue_ble write: <{}>".format(msg))
                bts = ""
                for c in msg:
                    bo = chr(mill_prot.add_odd_par(c))
                    bts += bo
                    btsx = bts.encode('latin1')
                log.debug("Sending: <{}>".format(btsx))
                try:
                    tx.write(btsx, withResponse=True)
                except Exception as e:
                    log.error(
                        "bluepy_ble: failed to write {}: {}".format(msg, e))
                wrque.task_done()

            rx.read()
            mil.waitForNotifications(0.02)
            # time.sleep(0.1)

        log.debug('wt-end')
