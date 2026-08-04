"""Хранилище связок. Здесь всё, что мост помнит между запусками."""


def test_связка_чата_и_темы_читается_в_обе_стороны(topics):
    topics.link(max_chat_id=386174042, topic_id=12, title="Александра Н")

    assert topics.topic_for_chat(386174042) == 12
    assert topics.chat_for_topic(12) == 386174042


def test_про_незнакомый_чат_честно_отвечает_ничем(topics):
    assert topics.topic_for_chat(999) is None
    assert topics.chat_for_topic(999) is None
    assert topics.tg_message_for(999, "1") is None
    assert topics.max_message_for(999) is None
    assert topics.last_outgoing(999) is None
    assert topics.delivered_until(999) is None


def test_тему_можно_перепривязать(topics):
    """Тему в Telegram удаляют руками — тогда мост заводит новую для того же чата."""
    topics.link(386174042, 12, "Александра Н")
    topics.link(386174042, 77, "Александра Н")

    assert topics.topic_for_chat(386174042) == 77
    assert topics.chat_for_topic(12) is None


def test_id_сообщения_не_зависит_от_числа_или_строки(topics):
    """MAX отдаёт id то числом, то строкой; для связки это должно быть одно и то же."""
    topics.pair_messages(386174042, 4453180454, tg_message_id=310)

    assert topics.tg_message_for(386174042, 4453180454) == 310
    assert topics.tg_message_for(386174042, "4453180454") == 310
    assert topics.max_message_for(310) == (386174042, "4453180454")


def test_одинаковые_id_в_разных_чатах_не_путаются(topics):
    topics.pair_messages(100, 5, tg_message_id=1)
    topics.pair_messages(200, 5, tg_message_id=2)

    assert topics.tg_message_for(100, 5) == 1
    assert topics.tg_message_for(200, 5) == 2


def test_отметка_о_доставке_не_едет_назад(topics):
    """Иначе после простоя мост заново притащил бы уже доставленное — по второму разу."""
    topics.remember_delivered(386174042, 1000)
    topics.remember_delivered(386174042, 900)

    assert topics.delivered_until(386174042) == 1000

    topics.remember_delivered(386174042, 1500)
    assert topics.delivered_until(386174042) == 1500


def test_помним_только_последнее_своё_сообщение(topics):
    """На него вешается «прочитано»: галочка нужна на свежем, а не на всех подряд."""
    topics.remember_outgoing(386174042, 5)
    topics.remember_outgoing(386174042, 9)

    assert topics.last_outgoing(386174042) == 9


def test_про_одну_версию_сообщаем_один_раз(topics):
    """Иначе при каждом перезапуске мост писал бы одно и то же, и его перестанут читать."""
    assert topics.already_announced("1.1.0") is False

    topics.remember_announced("1.1.0")

    assert topics.already_announced("1.1.0") is True
    assert topics.already_announced("1.2.0") is False


def test_база_переживает_переоткрытие(topics, tmp_path):
    from bridge.storage import TopicMap

    topics.link(386174042, 12, "Александра Н")
    topics.remember_delivered(386174042, 1000)

    reopened = TopicMap(tmp_path / "topics.db")

    assert reopened.topic_for_chat(386174042) == 12
    assert reopened.delivered_until(386174042) == 1000
