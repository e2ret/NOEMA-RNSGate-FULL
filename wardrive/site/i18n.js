window.i18n = {
  ru: {
    'nav.about':     'Что такое RNS',
    'nav.software':  'Клиенты',
    'nav.map':       'Карта',
    'nav.nodes':     'Узлы',
    'nav.hardware':  'Железо',
    'nav.flash':     'Прошивка',
    'nav.faq':       'FAQ',
    'nav.community': 'Сообщество',

    'hero.title':    'Московская сеть Reticulum',
    'hero.subtitle': 'Свободная, зашифрованная, децентрализованная mesh-сеть',
    'hero.cta1':     'Подключиться',
    'hero.cta2':     'Карта сети',
    'hero.cta3':     'Прошить устройство',

    'about.title': 'Что такое Reticulum?',
    'about.p1':    'Reticulum — криптографический сетевой стек с открытым исходным кодом для создания локальных и распределённых сетей. Работает поверх LoRa, WiFi HaLow, обычного WiFi, Ethernet и последовательных каналов.',
    'about.p2':    'Московская сеть RNS объединяет энтузиастов, радиолюбителей и исследователей, строящих децентрализованную радиосеть, независимую от интернет-инфраструктуры.',
    'about.link':  '→ Официальная документация Reticulum',

    'feature.crypto.title': 'Шифрование',
    'feature.crypto.desc':  'E2E-шифрование на уровне протокола — без доверия к инфраструктуре',
    'feature.infra.title':  'Без инфраструктуры',
    'feature.infra.desc':   'Никаких серверов и центральных узлов. Сеть живёт пока работают узлы',
    'feature.hw.title':     'Любое железо',
    'feature.hw.desc':      'LoRa, WiFi HaLow, WiFi, serial, BLE — любой канал связи',
    'feature.range.title':  'Дальний радиус',
    'feature.range.desc':   'LoRa-узлы покрывают километры без ретрансляторов',

    'software.title':        'Клиентский софт',
    'software.sideband.desc':'Мобильный клиент для Android и iOS. Сообщения, файлы, карты.',
    'software.nomad.desc':   'Десктопный и CLI клиент. Форумы, чаты, страницы в сети Reticulum.',
    'software.ratspeak.desc':    'Нативный клиент для зашифрованных переписок, файлов, звонков и игр через Reticulum. Поддерживает LoRa, WiFi, BLE и TCP.',
    'software.meshchatx.desc':  'Универсальный клиент Reticulum с поддержкой сообщений, звонков и встроенным браузером Nomadnet. Доступен для macOS, Linux, Windows и Android.',
    'software.columba.desc':    'Нативное Android-приложение для общения через mesh-сети по Bluetooth LE, WiFi TCP или LoRa (RNode) на базе Reticulum и LXMF.',
    'software.inertia.desc':    'Нативный Swift-клиент для iOS на базе Reticulum, LXMF и NomadNet. Простой и приватный — для людей, а не машин.',
    'software.reticulummobile.desc': 'Нативный Android и iOS клиент на Kotlin Multiplatform.',
    'software.retichat.desc':   'LXMF-клиент для Android и iOS. Доступен в Google Play и Apple App Store.',
    'software.partyline.desc':  'Групповая голосовая связь для Reticulum на базе LXST. Графический и терминальный клиент плюс лёгкий сервер. Кодеки Opus и Codec2 — от быстрых каналов до LoRa.',
    'software.download':        'Скачать',

    'map.title':    'Московская сеть',
    'map.rf.title': 'Параметры радиоканала',
    'map.lora':     'LoRa',
    'map.halow':    'HaLow',
    'map.freq':     'Частота',
    'map.bw':       'Полоса',
    'map.sf':       'SF',
    'map.cr':       'CR',

    'map.fallback': '↗ Открыть карту в новой вкладке',
    'map.coverage': 'Карта покрытия',

    'nodes.title':    'Узлы сети',
    'nodes.hostname': 'Хост',
    'nodes.address':  'Адрес',
    'nodes.protocol': 'Протокол',
    'nodes.uptime':   'Аптайм (48ч)',
    'nodes.status':   'Статус',
    'nodes.loading':  'Загрузка...',
    'nodes.error':    'Не удалось загрузить список узлов',
    'nodes.refresh':  '// данные обновляются каждые 10 минут',
    'nodes.updated':  'обновлено',
    'nodes.online':   'онлайн',
    'nodes.degraded': 'нет анонсов',
    'nodes.offline':  'офлайн',
    'nodes.blank':    '—',
    'nodes.empty':    'Нет данных об узлах',
    'nodes.copycfg':  'CFG',

    'hw.title':          'Железо и сборки',
    'hw.rnode.title':    'RNode DIY',
    'hw.rnode.desc':     'Соберите собственный узел на базе ESP32 или nRF52840 и LoRa-модуля. Полностью открытая схема, прошивка на GitHub. Стоимость компонентов — от 1500 руб.',
    'hw.rnode.link':     'Подробнее',
    'hw.halow.title':    'HaLow модуль',
    'hw.halow.desc':     'WiFi HaLow (802.11ah) на 866 МГц обеспечивает дальность до нескольких километров при сохранении скорости. Подходит для стационарных узлов.',
    'hw.halow.link':     'Подробнее',
    'hw.rnsbox.title':   'RNSBox',
    'hw.rnsbox.desc':    'Компактный узел Reticulum на базе Sipeed LicheeRV Nano. Работает под Linux с полной поддержкой Reticulum Network Stack.',
    'hw.rnsbox.link':    'Подробнее',
    'hw.prebuilt.title': 'Готовые узлы',
    'hw.prebuilt.desc':  'Список проверенных готовых устройств, совместимых с Reticulum и прошивкой RNode. Подходит тем, кто не хочет паять.',
    'hw.prebuilt.link':  'Подробнее',

    'flash.title': 'Прошейте своё устройство',
    'flash.desc':  'Используйте наш веб-прошивальщик для настройки RNode прямо в браузере. Подключите устройство по USB — никаких дополнительных инструментов не нужно.',
    'flash.btn':   'Открыть веб-прошивальщик',

    'faq.title':   'Частые вопросы',
    'faq.q1.q':    'Что такое Reticulum?',
    'faq.q1.a':    'Reticulum — криптографический сетевой стек с открытым исходным кодом, разработанный Марком Квистом (Mark Qvist). Он обеспечивает шифрование, адресацию и маршрутизацию поверх любого физического канала — от LoRa до Ethernet — без центральных серверов.',
    'faq.q2.q':    'Какое оборудование нужно?',
    'faq.q2.a':    'Для начала достаточно компьютера или смартфона с WiFi. Для радио-узла нужен ESP32 или Raspberry Pi с LoRa-модулем на базе чипов Semtech серий SX126X, SX127X или SX128X, либо WiFi HaLow адаптер. Готовые RNode можно купить или собрать по открытой схеме.',
    'faq.q3.q':    'Как подключиться к московской сети?',
    'faq.q3.a':    'Установите Sideband или Nomad Network, настройте TCP-соединение с одним из узлов из списка, или разверните собственный LoRa-узел поблизости. Карта покрытия покажет ближайшие точки входа.',
    'faq.q4.q':    'Это бесплатно?',
    'faq.q4.a':    'Да. Reticulum — полностью открытый проект под лицензией MIT. Московская инфраструктура поддерживается добровольцами на собственном оборудовании.',
    'faq.q5.q':    'Нужна ли лицензия на радио?',
    'faq.q5.a':    'LoRa на 868 МГц и WiFi HaLow на 866 МГц работают в ISM-диапазоне, разрешённом для использования без лицензии. Мощность передатчика ограничена регуляторными нормами (обычно до 25 мВт ERP).',
    'faq.q6.q':    'Как связаться с сообществом?',
    'faq.q6.a':    'Присоединяйтесь к Telegram-группам ниже. Основная группа RNS Moscow — для обсуждения техники, сборок и развития сети.',
    'faq.q7.q':    'Я не понимаю, как работают и распространяются анонсы',
    'faq.q7.a':    'Воспользуйтесь интерактивным симулятором, который наглядно показывает, как анонсы распространяются по сети Reticulum — от узла к узлу, через ретрансляторы и с учётом числа прыжков. <a href="announce-sim.html">Открыть симулятор распространения анонсов →</a>',

    'community.title':        'Сообщество',
    'community.rnsmsk.name':  'Reticulum Moscow',
    'community.rnsmsk.desc':  'Основная Telegram-группа московского Reticulum-сообщества',
    'community.rnsrus.name':  'Reticulum Russia',
    'community.rnsrus.desc':  'Общероссийское Telegram-сообщество Reticulum',
    'community.rnsru.name':     'Reticulum SPB',
    'community.rnsru.desc':     'Санкт-Петербургское Telegram-сообщество Reticulum',
    'community.recipes.name':   'rns.recipes Community forum',
    'community.recipes.desc':   'Форум сообщества, доступный как через Nomadnet, так и через интернет.',
    'community.nomaddir.name':  'Nomadnet Каталог',
    'community.nomaddir.desc':  'Список русскоязычных сайтов и страниц в сети Nomadnet.',
    'community.rrcdir.name':    'RRC Каталог',
    'community.rrcdir.desc':    'Список известных RRC-хабов.',
    'community.plndir.name':    'Partyline Каталог',
    'community.plndir.desc':    'Список известных Partyline-серверов.',
    'community.open':           'Открыть',

    'nomaddir.title':       'Nomadnet Каталог',
    'nomaddir.subtitle':    'Русскоязычные сайты и страницы в сети Nomadnet.',
    'nomaddir.info':        'Страницы Nomadnet — текстовые, малополосные, по духу схожи с BBS и ранним интернетом. Посетить их можно, воспользовавшись Nomadnet-браузером, доступным в некоторых клиентах (<a href="https://github.com/torlando-tech/columba/" target="_blank" rel="noopener">Columba</a>, <a href="https://github.com/fr33n0w/rBrowser" target="_blank" rel="noopener">rBrowser</a>, <a href="https://github.com/markqvist/NomadNet" target="_blank" rel="noopener">Nomadnet</a>, <a href="https://meshchatx.com/" target="_blank" rel="noopener">MeshChatX</a>).',
    'nomaddir.col.name':    'Название',
    'nomaddir.col.address': 'Адрес',
    'nomaddir.col.desc':    'Описание',

    'rrcdir.title':       'RRC Каталог',
    'rrcdir.subtitle':    'Известные RRC-хабы в сети Reticulum.',
    'rrcdir.info':        'RRC (Reticulum Relay Chat) — протокол групповых чатов для сети Reticulum. Хабы выступают ретрансляторами, хранящими и пересылающими сообщения подключённым клиентам в формате каналов. Поддерживается клиентами <a href="https://github.com/markqvist/NomadNet" target="_blank" rel="noopener">Nomadnet</a>, <a href="https://github.com/torlando-tech/eridanus" target="_blank" rel="noopener">Eridanus</a> и <a href="https://meshchatx.com/" target="_blank" rel="noopener">MeshChatX</a>.',
    'rrcdir.col.name':    'Название',
    'rrcdir.col.address': 'Адрес',
    'rrcdir.col.desc':    'Описание',

    'plndir.title':       'Partyline Каталог',
    'plndir.subtitle':    'Известные Partyline-серверы и их комнаты в сети Reticulum.',
    'plndir.info':        '<a href="https://github.com/RFnexus/partyline" target="_blank" rel="noopener">Partyline</a> — приложение группового голосового общения для Reticulum, построенное на <a href="https://github.com/markqvist/LXST" target="_blank" rel="noopener">LXST</a> и вдохновлённое Mumble. Серверы содержат комнаты для зашифрованных голосовых разговоров в реальном времени поверх любого транспорта Reticulum — от быстрых каналов до низкоскоростных LoRa-линков — с адаптивными кодеками Opus и Codec2 от 700 бит/с до 56 кбит/с. Комнаты могут быть открытыми, защищёнными паролем, привязанными к идентичностям или к списку разрешённых, с режимом push-to-talk, рассчитанным на медленные радиоканалы. Доступны графический и терминальный клиенты, а также дозвон в комнату из любого LXST-клиента.',
    'plndir.col.name':    'Название',
    'plndir.col.number':  'Номер (для дозвона)',
    'plndir.col.dest':    'Хеш назначения',
    'plndir.col.desc':    'Описание',
    'plndir.hint':        'Сервер добавляется в клиент Partyline по хешу назначения. Номера комнат — то, что набирают rnphone, Sideband и другие LXST-клиенты, чтобы попасть сразу в нужную комнату.',
    'plndir.credit':      'Иллюстрация из проекта <a href="https://github.com/RFnexus/partyline" target="_blank" rel="noopener">Partyline</a> · рисунок ниточного телефона — <a href="https://openclipart.org/detail/351397/classic-string-telephone" target="_blank" rel="noopener">openclipart.org</a>',

    'footer.copy':    '© 2025–2026 rns.moscow — Московская сеть Reticulum',
    'footer.nomad':     'Nomad Network:',
    'footer.nomad.git': 'rngit узел:',
    'footer.map':     'Карта покрытия',
    'footer.flasher': 'Веб-прошивальщик',
    'footer.credits': '<a href="https://reticulum.network" target="_blank" rel="noopener">Reticulum Network Stack</a> разработан <a href="https://unsigned.io/contact.html" target="_blank" rel="noopener">Mark Qvist</a>',
    'footer.old':     'Старый сайт',

    'page.back':       '← Назад',
    'page.flash.note': 'Все перечисленные устройства можно прошить через браузер без установки дополнительных программ.',

    'devices.title':    'Готовые устройства',
    'devices.subtitle': 'Список устройств, совместимых с прошивкой RNode и поддерживаемых веб-прошивальщиком. Все устройства можно прошить напрямую через браузер без установки дополнительного программного обеспечения.',

    'rnsbox.title':          'RNSBox',
    'rnsbox.subtitle':       'Компактный узел Reticulum на базе Sipeed LicheeRV Nano.',
    'rnsbox.about.title':    'Что такое RNSBox?',
    'rnsbox.about.body':     'RNSBox — это готовый к использованию компактный узел Reticulum, построенный на базе одноплатного компьютера Sipeed LicheeRV Nano. Устройство работает под управлением Linux и запускает полноценный Reticulum Network Stack, обеспечивая стабильную работу в качестве шлюза сети.',
    'rnsbox.firmware.title': 'Прошивка',
    'rnsbox.firmware.desc':  'Скачайте последнюю версию образа для RNSBox.',
    'rnsbox.firmware.btn':   'Открыть директорию прошивок',

    'diy.title':           'DIY-устройства',
    'diy.subtitle':        'Самодельные и сообщественные устройства, совместимые с прошивкой RNode. Каждое устройство можно прошить через браузер без установки дополнительного программного обеспечения.',
    'diy.group.rns':       'rns.moscow — Разработки сообщества',
    'diy.group.community': 'Сообщество — DIY-платы',
    'diy.group.rnode':     'Unsigned.io — Оригинальный RNode',
  },

  en: {
    'nav.about':     'About',
    'nav.software':  'Software',
    'nav.map':       'Map',
    'nav.nodes':     'Nodes',
    'nav.hardware':  'Hardware',
    'nav.flash':     'Flash',
    'nav.faq':       'FAQ',
    'nav.community': 'Community',

    'hero.title':    'Moscow Reticulum Network',
    'hero.subtitle': 'Free, encrypted, decentralised mesh-network',
    'hero.cta1':     'Get Connected',
    'hero.cta2':     'Network Map',
    'hero.cta3':     'Flash Device',

    'about.title': 'What is Reticulum?',
    'about.p1':    'Reticulum is an open-source cryptography-based networking stack for building local and wide-area networks. It runs over LoRa, WiFi HaLow, standard WiFi, Ethernet, and serial channels.',
    'about.p2':    'The Moscow RNS network connects enthusiasts, radio hobbyists, and researchers building a decentralised radio mesh independent of internet infrastructure.',
    'about.link':  '→ Official Reticulum Documentation',

    'feature.crypto.title': 'Encryption',
    'feature.crypto.desc':  'E2E encryption at the protocol level — without trusting any infrastructure',
    'feature.infra.title':  'Infrastructure-free',
    'feature.infra.desc':   'No servers or central nodes. The network exists as long as nodes are running',
    'feature.hw.title':     'Any hardware',
    'feature.hw.desc':      'LoRa, WiFi HaLow, WiFi, serial, I2C — any communication channel',
    'feature.range.title':  'Long range',
    'feature.range.desc':   'LoRa nodes cover kilometres without repeaters',

    'software.title':        'Client Software',
    'software.sideband.desc':'Mobile client for Android and iOS. Messages, files, maps.',
    'software.nomad.desc':   'Desktop and CLI client. Forums, chats, pages on the Reticulum network.',
    'software.ratspeak.desc':    'Native desktop and mobile client for E2EE messaging, file sharing, voice calls, and games over Reticulum. Supports LoRa, WiFi, BLE and TCP.',
    'software.meshchatx.desc':  'An all-in-one Reticulum client featuring messaging, calling, and a Nomadnet browser. Available for macOS, Linux, Windows and Android.',
    'software.columba.desc':    'Native Android messaging app for mesh networks over Bluetooth LE, WiFi TCP, or LoRa (RNode), built on Reticulum and LXMF.',
    'software.inertia.desc':    'Pure Swift iOS client for Reticulum, LXMF and NomadNet. Simple, private, and built for humans.',
    'software.reticulummobile.desc': 'Native Android & iOS client, built in Kotlin Multiplatform.',
    'software.retichat.desc':   'LXMF chat client for Android and iOS. Available on Google Play and the App Store.',
    'software.partyline.desc':  'Group voice for Reticulum, built on LXST. GUI and terminal clients plus a lightweight server. Opus and Codec2 codecs, from fast links down to LoRa.',
    'software.download':        'Download',

    'map.title':    'Moscow Network',
    'map.rf.title': 'Radio Parameters',
    'map.lora':     'LoRa',
    'map.halow':    'HaLow',
    'map.freq':     'Frequency',
    'map.bw':       'Bandwidth',
    'map.sf':       'SF',
    'map.cr':       'CR',

    'map.fallback': '↗ Open map in new tab',
    'map.coverage': 'Coverage Map',

    'nodes.title':    'Network Nodes',
    'nodes.hostname': 'Hostname',
    'nodes.address':  'Address',
    'nodes.protocol': 'Protocol',
    'nodes.uptime':   'Uptime (48h)',
    'nodes.status':   'Status',
    'nodes.loading':  'Loading...',
    'nodes.error':    'Failed to load node list',
    'nodes.refresh':  '// data refreshes every 10 minutes',
    'nodes.updated':  'updated',
    'nodes.online':   'online',
    'nodes.degraded': 'no announces',
    'nodes.offline':  'offline',
    'nodes.blank':    '—',
    'nodes.empty':    'No node data available',
    'nodes.copycfg':  'CFG',

    'hw.title':          'Hardware & Builds',
    'hw.rnode.title':    'RNode DIY',
    'hw.rnode.desc':     'Build your own node using ESP32 or nRF52840 and a LoRa module. Fully open schematic, firmware on GitHub. Component cost from ~$15.',
    'hw.rnode.link':     'Learn more',
    'hw.halow.title':    'HaLow Module',
    'hw.halow.desc':     'WiFi HaLow (802.11ah) at 866 MHz provides range of several kilometres while maintaining decent throughput. Suitable for mains-powered fixed nodes.',
    'hw.halow.link':     'Learn more',
    'hw.rnsbox.title':   'RNSBox',
    'hw.rnsbox.desc':    'A compact Reticulum node based on the Sipeed LicheeRV Nano. Runs Linux with full Reticulum Network Stack support.',
    'hw.rnsbox.link':    'Learn more',
    'hw.prebuilt.title': 'Pre-built Nodes',
    'hw.prebuilt.desc':  'A curated list of verified devices compatible with Reticulum and RNode firmware. Ideal if you don\'t want to solder.',
    'hw.prebuilt.link':  'Learn more',

    'flash.title': 'Flash Your Device',
    'flash.desc':  'Use our web flasher to configure your RNode directly in the browser. Plug in via USB — no additional tools required.',
    'flash.btn':   'Open Web Flasher',

    'faq.title':   'FAQ',
    'faq.q1.q':    'What is Reticulum?',
    'faq.q1.a':    'Reticulum is an open-source cryptographic networking stack developed by Mark Qvist. It provides encryption, addressing and routing over any physical channel — from LoRa to Ethernet — without central servers.',
    'faq.q2.q':    'What hardware do I need?',
    'faq.q2.a':    'To start, a computer or smartphone with WiFi is sufficient. For a radio node you need an ESP32 or Raspberry Pi with a LoRa module based on Semtech SX126X, SX127X, or SX128X chips, or a WiFi HaLow adapter. Ready-made RNodes can be purchased or built from open schematics.',
    'faq.q3.q':    'How do I join the Moscow network?',
    'faq.q3.a':    'Install Sideband or Nomad Network, configure a TCP connection to one of the nodes in the list, or deploy your own LoRa node nearby. The coverage map shows nearest entry points.',
    'faq.q4.q':    'Is it free?',
    'faq.q4.a':    'Yes. Reticulum is a fully open project under the MIT license. The Moscow infrastructure is maintained by volunteers on their own equipment.',
    'faq.q5.q':    'Do I need a radio license?',
    'faq.q5.a':    'LoRa on 868 MHz and WiFi HaLow on 866 MHz operate in the ISM band, which is licence-free. Transmitter power is subject to regulatory limits (typically up to 25 mW ERP).',
    'faq.q6.q':    'How to contact the community?',
    'faq.q6.a':    'Join the Telegram groups below. The main RNS Moscow group is where hardware, builds, and network development are discussed.',
    'faq.q7.q':    'I don\'t understand how announces work and propagate',
    'faq.q7.a':    'Use the interactive simulator to see how announces spread through the Reticulum network — node by node, through repeaters, and with hop count. <a href="announce-sim.html">Open the Announce Propagation Simulator →</a>',

    'community.title':        'Community',
    'community.rnsmsk.name':  'Reticulum Moscow',
    'community.rnsmsk.desc':  'Main group of the Moscow Reticulum community',
    'community.rnsrus.name':  'Reticulum Russia',
    'community.rnsrus.desc':  'All-Russian Reticulum community',
    'community.rnsru.name':     'Reticulum SPB',
    'community.rnsru.desc':     'St. Petersburg Reticulum community',
    'community.recipes.name':   'rns.recipes Community forum',
    'community.recipes.desc':   'A community forum available both from Nomadnet and Internet.',
    'community.nomaddir.name':  'Nomadnet Directory',
    'community.nomaddir.desc':  'A list of Russian-language sites and pages on the Nomadnet network.',
    'community.rrcdir.name':    'RRC Directory',
    'community.rrcdir.desc':    'A list of known RRC hubs.',
    'community.plndir.name':    'Partyline Directory',
    'community.plndir.desc':    'A list of known Partyline servers.',
    'community.open':           'Open',

    'nomaddir.title':       'Nomadnet Directory',
    'nomaddir.subtitle':    'Russian-language sites and pages on the Nomadnet network.',
    'nomaddir.info':        'Nomadnet pages are text-style and bandwidth-efficient pages similar to BBS and early Internet services. To browse them, use a Nomadnet-capable client such as <a href="https://github.com/torlando-tech/columba/" target="_blank" rel="noopener">Columba</a>, <a href="https://github.com/fr33n0w/rBrowser" target="_blank" rel="noopener">rBrowser</a>, <a href="https://github.com/markqvist/NomadNet" target="_blank" rel="noopener">Nomadnet</a>, or <a href="https://meshchatx.com/" target="_blank" rel="noopener">MeshChatX</a>.',
    'nomaddir.col.name':    'Name',
    'nomaddir.col.address': 'Address',
    'nomaddir.col.desc':    'Description',

    'rrcdir.title':       'RRC Directory',
    'rrcdir.subtitle':    'Known RRC hubs on the Reticulum network.',
    'rrcdir.info':        'RRC (Reticulum Relay Chat) is a group chat protocol for the Reticulum network. Hubs act as relay points that store and forward messages to connected clients in a channel-based format. Supported by <a href="https://github.com/markqvist/NomadNet" target="_blank" rel="noopener">Nomadnet</a>, <a href="https://github.com/torlando-tech/eridanus" target="_blank" rel="noopener">Eridanus</a>, and <a href="https://meshchatx.com/" target="_blank" rel="noopener">MeshChatX</a>.',
    'rrcdir.col.name':    'Name',
    'rrcdir.col.address': 'Address',
    'rrcdir.col.desc':    'Description',

    'plndir.title':       'Partyline Directory',
    'plndir.subtitle':    'Known Partyline servers and their rooms on the Reticulum network.',
    'plndir.info':        '<a href="https://github.com/RFnexus/partyline" target="_blank" rel="noopener">Partyline</a> is a group voice application for Reticulum, built on <a href="https://github.com/markqvist/LXST" target="_blank" rel="noopener">LXST</a> and inspired by Mumble. Servers host rooms for encrypted, real-time group voice over any Reticulum transport — from fast links down to low-bandwidth LoRa — using adaptive Opus and Codec2 codecs from 700 bps to 56 kbps. Rooms can be open, password-protected, identity-based or allowlisted, with push-to-talk tuned for slow radio links. Both a GUI and a terminal client are available, and rooms can also be dialled into from any LXST client.',
    'plndir.col.name':    'Name',
    'plndir.col.number':  'Number (dial this)',
    'plndir.col.dest':    'Destination hash',
    'plndir.col.desc':    'Description',
    'plndir.hint':        'Add a server to the Partyline client by its destination hash. Room numbers are what rnphone, Sideband and other LXST clients dial to land straight in that room.',
    'plndir.credit':      'Artwork from the <a href="https://github.com/RFnexus/partyline" target="_blank" rel="noopener">Partyline</a> project · tin can telephone drawing from <a href="https://openclipart.org/detail/351397/classic-string-telephone" target="_blank" rel="noopener">openclipart.org</a>',

    'footer.copy':    '© 2025–2026 rns.moscow — Moscow Reticulum Network',
    'footer.nomad':     'Nomad Network:',
    'footer.nomad.git': 'rngit node:',
    'footer.map':     'Coverage Map',
    'footer.flasher': 'Web Flasher',
    'footer.credits': '<a href="https://reticulum.network" target="_blank" rel="noopener">Reticulum Network Stack</a> is created by <a href="https://unsigned.io/contact.html" target="_blank" rel="noopener">Mark Qvist</a>',
    'footer.old':     'Old Website',

    'page.back':       '← Back',
    'page.flash.note': 'All listed devices can be flashed from the browser without installing any additional software.',

    'devices.title':    'Pre-built Devices',
    'devices.subtitle': 'Devices compatible with RNode firmware and supported by the web flasher. All devices can be flashed directly from the browser with no additional software required.',

    'rnsbox.title':          'RNSBox',
    'rnsbox.subtitle':       'A compact Reticulum node based on the Sipeed LicheeRV Nano.',
    'rnsbox.about.title':    'What is RNSBox?',
    'rnsbox.about.body':     'RNSBox is a ready-to-use compact Reticulum node built around the Sipeed LicheeRV Nano single-board computer. It runs Linux and a full Reticulum Network Stack instance, making it a reliable always-on network gateway.',
    'rnsbox.firmware.title': 'Firmware',
    'rnsbox.firmware.desc':  'Download the latest RNSBox image.',
    'rnsbox.firmware.btn':   'Open firmware directory',

    'diy.title':           'DIY Devices',
    'diy.subtitle':        'Handmade and community devices compatible with RNode firmware. Each device can be flashed from the browser without installing any additional software.',
    'diy.group.rns':       'rns.moscow — Community Builds',
    'diy.group.community': 'Community — DIY Boards',
    'diy.group.rnode':     'Unsigned.io — Original RNode',
  }
};

let _lang = 'ru';

window.setLang = function(lang) {
  _lang = lang;
  document.documentElement.lang = lang;
  document.title = lang === 'ru'
    ? 'RNS.MOSCOW — Московская сеть Reticulum'
    : 'RNS.MOSCOW — Moscow Reticulum Network';

  document.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.getAttribute('data-i18n');
    const val = window.i18n[lang][key];
    if (val !== undefined) el.innerHTML = val;
  });

  document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
    const key = el.getAttribute('data-i18n-placeholder');
    const val = window.i18n[lang][key];
    if (val !== undefined) el.placeholder = val;
  });

  const btnRu = document.getElementById('btn-ru');
  const btnEn = document.getElementById('btn-en');
  if (btnRu) btnRu.classList.toggle('active', lang === 'ru');
  if (btnEn) btnEn.classList.toggle('active', lang === 'en');

  // Tell the embedded coverage map which language to label places in, so it matches
  // the page instead of following the browser header. Targeted at the map's own origin
  // rather than '*', and wrapped because the iframe may not be reachable yet (it is
  // loading="lazy"); index.html re-sends on its load event to cover that.
  var mapFrame = document.querySelector('.map-frame-wrapper iframe');
  if (mapFrame && mapFrame.contentWindow) {
    try {
      mapFrame.contentWindow.postMessage({ type: 'lang', lang: lang },
                                         'https://map.rns.moscow');
    } catch (e) { /* not loaded yet */ }
  }

  if (window.renderNodes) window.renderNodes();
};

window.getLang = function() { return _lang; };
window.t = function(key) {
  return (window.i18n[_lang] && window.i18n[_lang][key]) || key;
};
