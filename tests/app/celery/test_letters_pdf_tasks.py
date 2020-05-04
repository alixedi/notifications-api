from unittest.mock import call

import boto3
from datetime import datetime, timedelta
from moto import mock_s3
from flask import current_app
from freezegun import freeze_time
import pytest
from botocore.exceptions import ClientError
from celery.exceptions import MaxRetriesExceededError
from sqlalchemy.orm.exc import NoResultFound

from app import encryption
from app.errors import VirusScanError
from app.exceptions import NotificationTechnicalFailureException
from app.celery.letters_pdf_tasks import (
    create_letters_pdf,
    collate_letter_pdfs_to_be_sent,
    get_key_and_size_of_letters_to_be_sent_to_print,
    group_letters,
    process_sanitised_letter,
    process_virus_scan_failed,
    process_virus_scan_error,
    replay_letters_in_error,
    sanitise_letter,
    update_billable_units_for_letter,
    _move_invalid_letter_and_update_status,
)
from app.config import QueueNames, TaskNames
from app.letters.utils import ScanErrorType
from app.models import (
    KEY_TYPE_NORMAL,
    KEY_TYPE_TEST,
    Notification,
    NOTIFICATION_CREATED,
    NOTIFICATION_DELIVERED,
    NOTIFICATION_PENDING_VIRUS_CHECK,
    NOTIFICATION_TECHNICAL_FAILURE,
    NOTIFICATION_VALIDATION_FAILED,
    NOTIFICATION_VIRUS_SCAN_FAILED,
)

from tests.app.db import create_notification, create_letter_branding

from tests.conftest import set_config_values


def test_should_have_decorated_tasks_functions():
    assert create_letters_pdf.__wrapped__.__name__ == 'create_letters_pdf'
    assert collate_letter_pdfs_to_be_sent.__wrapped__.__name__ == 'collate_letter_pdfs_to_be_sent'
    assert process_virus_scan_failed.__wrapped__.__name__ == 'process_virus_scan_failed'
    assert process_virus_scan_error.__wrapped__.__name__ == 'process_virus_scan_error'
    assert sanitise_letter.__wrapped__.__name__ == 'sanitise_letter'
    assert process_sanitised_letter.__wrapped__.__name__ == 'process_sanitised_letter'


def test_create_letters_pdf(mocker, sample_letter_notification):
    mock_celery = mocker.patch('app.celery.letters_pdf_tasks.notify_celery.send_task')
    mocker.patch('app.celery.letters_pdf_tasks.get_letter_pdf_filename', return_value='LETTER.PDF')
    create_letters_pdf(sample_letter_notification.id)

    letter_data = {
        'letter_contact_block': sample_letter_notification.reply_to_text,
        'template': {
            "subject": sample_letter_notification.template.subject,
            "content": sample_letter_notification.template.content,
            "template_type": sample_letter_notification.template.template_type
        },
        'values': sample_letter_notification.personalisation,
        'logo_filename': None,  # no logo
        'letter_filename': 'LETTER.PDF',
        "notification_id": str(sample_letter_notification.id),
        'key_type': sample_letter_notification.key_type
    }

    encrypted_data = encryption.encrypt(letter_data)

    mock_celery.assert_called_once_with(
        name=TaskNames.CREATE_LETTER_PDF,
        args=(encrypted_data,),
        queue=QueueNames.SANITISE_LETTERS
    )


def test_create_letters_pdf_non_existent_notification(notify_api, mocker, fake_uuid):
    with pytest.raises(expected_exception=NoResultFound):
        create_letters_pdf(fake_uuid)


def test_create_letters_pdf_retries_upon_error(mocker, sample_letter_notification):
    mock_celery = mocker.patch('app.celery.letters_pdf_tasks.notify_celery.send_task', side_effect=Exception())
    mocker.patch('app.celery.letters_pdf_tasks.get_letter_pdf_filename', return_value='LETTER.PDF')
    mock_retry = mocker.patch('app.celery.letters_pdf_tasks.create_letters_pdf.retry')

    create_letters_pdf(sample_letter_notification.id)

    assert mock_celery.called
    assert mock_retry.called


def test_create_letters_pdf_sets_technical_failure_max_retries(mocker, sample_letter_notification):
    mock_celery = mocker.patch('app.celery.letters_pdf_tasks.notify_celery.send_task', side_effect=Exception())
    mocker.patch('app.celery.letters_pdf_tasks.get_letter_pdf_filename', return_value='LETTER.PDF')
    mock_retry = mocker.patch(
        'app.celery.letters_pdf_tasks.create_letters_pdf.retry', side_effect=MaxRetriesExceededError)
    mock_update_noti = mocker.patch('app.celery.letters_pdf_tasks.update_notification_status_by_id')

    with pytest.raises(NotificationTechnicalFailureException):
        create_letters_pdf(sample_letter_notification.id)

    assert mock_celery.called
    assert mock_retry.called
    mock_update_noti.assert_called_once_with(sample_letter_notification.id, 'technical-failure')


# We only need this while we are migrating to the new letter_branding model
def test_create_letters_gets_the_right_logo_when_service_has_letter_branding_logo(
        notify_api, mocker, sample_letter_notification
):
    letter_branding = create_letter_branding(name='test brand', filename='test-brand')
    sample_letter_notification.service.letter_branding = letter_branding
    mock_celery = mocker.patch('app.celery.letters_pdf_tasks.notify_celery.send_task')
    mocker.patch('app.celery.letters_pdf_tasks.get_letter_pdf_filename', return_value='LETTER.PDF')
    create_letters_pdf(sample_letter_notification.id)

    letter_data = {
        'letter_contact_block': sample_letter_notification.reply_to_text,
        'template': {
            "subject": sample_letter_notification.template.subject,
            "content": sample_letter_notification.template.content,
            "template_type": sample_letter_notification.template.template_type
        },
        'values': sample_letter_notification.personalisation,
        'logo_filename': sample_letter_notification.service.letter_branding.filename,
        'letter_filename': 'LETTER.PDF',
        "notification_id": str(sample_letter_notification.id),
        'key_type': sample_letter_notification.key_type
    }

    encrypted_data = encryption.encrypt(letter_data)

    mock_celery.assert_called_once_with(
        name=TaskNames.CREATE_LETTER_PDF,
        args=(encrypted_data,),
        queue=QueueNames.SANITISE_LETTERS
    )


def test_update_billable_units_for_letter(mocker, sample_letter_notification):
    update_billable_units_for_letter(sample_letter_notification.id, 2)
    noti = Notification.query.filter(Notification.reference == sample_letter_notification.reference).one()
    assert noti.billable_units == 1


@freeze_time('2020-02-17 18:00:00')
def test_get_key_and_size_of_letters_to_be_sent_to_print(notify_api, mocker, sample_letter_template):
    create_notification(
        template=sample_letter_template,
        status='created',
        reference='ref0',
        created_at=(datetime.now() - timedelta(hours=2))
    )

    create_notification(
        template=sample_letter_template,
        status='created',
        reference='ref1',
        created_at=(datetime.now() - timedelta(hours=3))
    )

    create_notification(
        template=sample_letter_template,
        status='created',
        reference='ref2',
        created_at=(datetime.now() - timedelta(days=2))
    )

    # notifications we don't expect to get sent to print as they are in the wrong status
    for status in ['delivered', 'validation-failed', 'cancelled', 'sending']:
        create_notification(
            template=sample_letter_template,
            status=status,
            reference='ref3',
            created_at=(datetime.now() - timedelta(days=2))
        )

    # notification we don't expect to get sent as instead will make into this evenings print run
    create_notification(
        template=sample_letter_template,
        status='created',
        reference='ref4',
        created_at=(datetime.now() - timedelta(minutes=1))
    )

    # test notification we don't expect to get sent
    create_notification(
        template=sample_letter_template,
        status='created',
        reference='ref4',
        created_at=(datetime.now() - timedelta(days=1)),
        key_type=KEY_TYPE_TEST
    )

    mock_s3 = mocker.patch('app.celery.tasks.s3.head_s3_object', side_effect=[
        {'ContentLength': 2},
        {'ContentLength': 1},
        {'ContentLength': 3},
    ])

    results = get_key_and_size_of_letters_to_be_sent_to_print(datetime.now() - timedelta(minutes=30))

    assert mock_s3.call_count == 3
    mock_s3.assert_has_calls(
        [
            call(current_app.config['LETTERS_PDF_BUCKET_NAME'], '2020-02-16/NOTIFY.REF2.D.2.C.C.20200215180000.PDF'),
            call(current_app.config['LETTERS_PDF_BUCKET_NAME'], '2020-02-17/NOTIFY.REF1.D.2.C.C.20200217150000.PDF'),
            call(current_app.config['LETTERS_PDF_BUCKET_NAME'], '2020-02-17/NOTIFY.REF0.D.2.C.C.20200217160000.PDF'),
        ]
    )

    assert len(results) == 3
    assert results == [
        {'Key': '2020-02-16/NOTIFY.REF2.D.2.C.C.20200215180000.PDF', 'Size': 2},
        {'Key': '2020-02-17/NOTIFY.REF1.D.2.C.C.20200217150000.PDF', 'Size': 1},
        {'Key': '2020-02-17/NOTIFY.REF0.D.2.C.C.20200217160000.PDF', 'Size': 3},
    ]


@freeze_time('2020-02-17 18:00:00')
def test_get_key_and_size_of_letters_to_be_sent_to_print_catches_exception(
    notify_api, mocker, sample_letter_template
):
    create_notification(
        template=sample_letter_template,
        status='created',
        reference='ref0',
        created_at=(datetime.now() - timedelta(hours=2))
    )

    create_notification(
        template=sample_letter_template,
        status='created',
        reference='ref1',
        created_at=(datetime.now() - timedelta(hours=3))
    )
    error_response = {
        'Error': {
            'Code': 'FileNotFound',
            'Message': 'some error message from amazon',
            'Type': 'Sender'
        }
    }
    mock_head_s3_object = mocker.patch('app.celery.tasks.s3.head_s3_object', side_effect=[
        {'ContentLength': 2},
        ClientError(error_response, "File not found")
    ])

    results = get_key_and_size_of_letters_to_be_sent_to_print(datetime.now() - timedelta(minutes=30))

    assert mock_head_s3_object.call_count == 2
    mock_head_s3_object.assert_has_calls(
        [
            call(current_app.config['LETTERS_PDF_BUCKET_NAME'], '2020-02-17/NOTIFY.REF1.D.2.C.C.20200217150000.PDF'),
            call(current_app.config['LETTERS_PDF_BUCKET_NAME'], '2020-02-17/NOTIFY.REF0.D.2.C.C.20200217160000.PDF'),
        ]
    )

    assert results == [{'Key': '2020-02-17/NOTIFY.REF1.D.2.C.C.20200217150000.PDF', 'Size': 2}]


@pytest.mark.parametrize('time_to_run_task', [
    "2020-02-17 18:00:00",  # after 5:30pm
    "2020-02-18 02:00:00",  # the next day after midnight, before 5:30pm we expect the same results
])
def test_collate_letter_pdfs_to_be_sent(notify_api, sample_letter_template, mocker, time_to_run_task):
    with freeze_time("2020-02-17 18:00:00"):
        create_notification(
            template=sample_letter_template,
            status='created',
            reference='ref0',
            created_at=(datetime.now() - timedelta(hours=2))
        )

        create_notification(
            template=sample_letter_template,
            status='created',
            reference='ref1',
            created_at=(datetime.now() - timedelta(hours=3))
        )

        create_notification(
            template=sample_letter_template,
            status='created',
            reference='ref2',
            created_at=(datetime.now() - timedelta(days=2))
        )

    mocker.patch('app.celery.tasks.s3.head_s3_object', side_effect=[
        {'ContentLength': 2},
        {'ContentLength': 1},
        {'ContentLength': 3},
    ])

    mock_celery = mocker.patch('app.celery.letters_pdf_tasks.notify_celery.send_task')

    with set_config_values(notify_api, {'MAX_LETTER_PDF_COUNT_PER_ZIP': 2}):
        with freeze_time(time_to_run_task):
            collate_letter_pdfs_to_be_sent()

    assert len(mock_celery.call_args_list) == 2
    assert mock_celery.call_args_list[0] == call(
        name='zip-and-send-letter-pdfs',
        kwargs={
            'filenames_to_zip': [
                '2020-02-16/NOTIFY.REF2.D.2.C.C.20200215180000.PDF',
                '2020-02-17/NOTIFY.REF1.D.2.C.C.20200217150000.PDF'
            ],
            'upload_filename': 'NOTIFY.2020-02-17.001.k3x_WqC5KhB6e2DWv9Ma.ZIP'
        },
        queue='process-ftp-tasks',
        compression='zlib'
    )
    assert mock_celery.call_args_list[1] == call(
        name='zip-and-send-letter-pdfs',
        kwargs={
            'filenames_to_zip': [
                '2020-02-17/NOTIFY.REF0.D.2.C.C.20200217160000.PDF'
            ],
            'upload_filename': 'NOTIFY.2020-02-17.002.J85cUw-FWlKuAIOcwdLS.ZIP'
        },
        queue='process-ftp-tasks',
        compression='zlib'
    )


def test_group_letters_splits_on_file_size(notify_api):
    letters = [
        # ends under max but next one is too big
        {'Key': 'A.pdf', 'Size': 1}, {'Key': 'B.pdf', 'Size': 2},
        # ends on exactly max
        {'Key': 'C.pdf', 'Size': 3}, {'Key': 'D.pdf', 'Size': 1}, {'Key': 'E.pdf', 'Size': 1},
        # exactly max goes in next file
        {'Key': 'F.pdf', 'Size': 5},
        # if it's bigger than the max, still gets included
        {'Key': 'G.pdf', 'Size': 6},
        # whatever's left goes in last list
        {'Key': 'H.pdf', 'Size': 1}, {'Key': 'I.pdf', 'Size': 1},
    ]

    with set_config_values(notify_api, {'MAX_LETTER_PDF_ZIP_FILESIZE': 5}):
        x = group_letters(letters)

        assert next(x) == [{'Key': 'A.pdf', 'Size': 1}, {'Key': 'B.pdf', 'Size': 2}]
        assert next(x) == [{'Key': 'C.pdf', 'Size': 3}, {'Key': 'D.pdf', 'Size': 1}, {'Key': 'E.pdf', 'Size': 1}]
        assert next(x) == [{'Key': 'F.pdf', 'Size': 5}]
        assert next(x) == [{'Key': 'G.pdf', 'Size': 6}]
        assert next(x) == [{'Key': 'H.pdf', 'Size': 1}, {'Key': 'I.pdf', 'Size': 1}]
        # make sure iterator is exhausted
        assert next(x, None) is None


def test_group_letters_splits_on_file_count(notify_api):
    letters = [
        {'Key': 'A.pdf', 'Size': 1},
        {'Key': 'B.pdf', 'Size': 2},
        {'Key': 'C.pdf', 'Size': 3},
        {'Key': 'D.pdf', 'Size': 1},
        {'Key': 'E.pdf', 'Size': 1},
        {'Key': 'F.pdf', 'Size': 5},
        {'Key': 'G.pdf', 'Size': 6},
        {'Key': 'H.pdf', 'Size': 1},
        {'Key': 'I.pdf', 'Size': 1},
    ]

    with set_config_values(notify_api, {'MAX_LETTER_PDF_COUNT_PER_ZIP': 3}):
        x = group_letters(letters)

        assert next(x) == [{'Key': 'A.pdf', 'Size': 1}, {'Key': 'B.pdf', 'Size': 2}, {'Key': 'C.pdf', 'Size': 3}]
        assert next(x) == [{'Key': 'D.pdf', 'Size': 1}, {'Key': 'E.pdf', 'Size': 1}, {'Key': 'F.pdf', 'Size': 5}]
        assert next(x) == [{'Key': 'G.pdf', 'Size': 6}, {'Key': 'H.pdf', 'Size': 1}, {'Key': 'I.pdf', 'Size': 1}]
        # make sure iterator is exhausted
        assert next(x, None) is None


def test_group_letters_splits_on_file_size_and_file_count(notify_api):
    letters = [
        # ends under max file size but next file is too big
        {'Key': 'A.pdf', 'Size': 1},
        {'Key': 'B.pdf', 'Size': 2},
        # ends on exactly max number of files and file size
        {'Key': 'C.pdf', 'Size': 3},
        {'Key': 'D.pdf', 'Size': 1},
        {'Key': 'E.pdf', 'Size': 1},
        # exactly max file size goes in next file
        {'Key': 'F.pdf', 'Size': 5},
        # file size is within max but number of files reaches limit
        {'Key': 'G.pdf', 'Size': 1},
        {'Key': 'H.pdf', 'Size': 1},
        {'Key': 'I.pdf', 'Size': 1},
        # whatever's left goes in last list
        {'Key': 'J.pdf', 'Size': 1},
    ]

    with set_config_values(notify_api, {
        'MAX_LETTER_PDF_ZIP_FILESIZE': 5,
        'MAX_LETTER_PDF_COUNT_PER_ZIP': 3
    }):
        x = group_letters(letters)

        assert next(x) == [{'Key': 'A.pdf', 'Size': 1}, {'Key': 'B.pdf', 'Size': 2}]
        assert next(x) == [{'Key': 'C.pdf', 'Size': 3}, {'Key': 'D.pdf', 'Size': 1}, {'Key': 'E.pdf', 'Size': 1}]
        assert next(x) == [{'Key': 'F.pdf', 'Size': 5}]
        assert next(x) == [{'Key': 'G.pdf', 'Size': 1}, {'Key': 'H.pdf', 'Size': 1}, {'Key': 'I.pdf', 'Size': 1}]
        assert next(x) == [{'Key': 'J.pdf', 'Size': 1}]
        # make sure iterator is exhausted
        assert next(x, None) is None


@pytest.mark.parametrize('key', ["A.ZIP", "B.zip"])
def test_group_letters_ignores_non_pdfs(key):
    letters = [{'Key': key, 'Size': 1}]
    assert list(group_letters(letters)) == []


@pytest.mark.parametrize('key', ["A.PDF", "B.pdf", "C.PdF"])
def test_group_letters_includes_pdf_files(key):
    letters = [{'Key': key, 'Size': 1}]
    assert list(group_letters(letters)) == [[{'Key': key, 'Size': 1}]]


def test_group_letters_with_no_letters():
    assert list(group_letters([])) == []


def test_move_invalid_letter_and_update_status_logs_error_and_sets_tech_failure_state_if_s3_error(
    mocker,
    sample_letter_notification,
):
    error_response = {
        'Error': {
            'Code': 'InvalidParameterValue',
            'Message': 'some error message from amazon',
            'Type': 'Sender'
        }
    }
    mocker.patch('app.celery.letters_pdf_tasks.move_scan_to_invalid_pdf_bucket',
                 side_effect=ClientError(error_response, 'operation_name'))
    mock_logger = mocker.patch('app.celery.tasks.current_app.logger.exception')

    with pytest.raises(NotificationTechnicalFailureException):
        _move_invalid_letter_and_update_status(
            notification=sample_letter_notification,
            filename='filename',
            scan_pdf_object=mocker.Mock()
        )

    assert sample_letter_notification.status == NOTIFICATION_TECHNICAL_FAILURE
    mock_logger.assert_called_once_with(
        'Error when moving letter with id {} to invalid PDF bucket'.format(sample_letter_notification.id)
    )


def test_sanitise_letter_calls_template_preview_sanitise_task(mocker, sample_letter_notification):
    mock_celery = mocker.patch('app.celery.letters_pdf_tasks.notify_celery.send_task')
    filename = 'NOTIFY.{}'.format(sample_letter_notification.reference)
    sample_letter_notification.status = NOTIFICATION_PENDING_VIRUS_CHECK

    sanitise_letter(filename)

    mock_celery.assert_called_once_with(
        name=TaskNames.SANITISE_LETTER,
        kwargs={'notification_id': str(sample_letter_notification.id), 'filename': filename},
        queue=QueueNames.SANITISE_LETTERS,
    )


def test_sanitise_letter_does_not_call_template_preview_sanitise_task_if_notification_in_wrong_state(
    mocker,
    sample_letter_notification,
):
    mock_celery = mocker.patch('app.celery.letters_pdf_tasks.notify_celery.send_task')
    filename = 'NOTIFY.{}'.format(sample_letter_notification.reference)

    sanitise_letter(filename)

    assert not mock_celery.called


def test_sanitise_letter_does_not_call_template_preview_sanitise_task_if_there_is_an_exception(
    mocker,
    sample_letter_notification,
):
    mocker.patch('app.celery.letters_pdf_tasks.notify_celery.send_task', side_effect=Exception())
    mock_celery_retry = mocker.patch('app.celery.letters_pdf_tasks.sanitise_letter.retry')

    filename = 'NOTIFY.{}'.format(sample_letter_notification.reference)
    sample_letter_notification.status = NOTIFICATION_PENDING_VIRUS_CHECK

    sanitise_letter(filename)

    mock_celery_retry.assert_called_once_with(queue='retry-tasks')


def test_sanitise_letter_puts_letter_into_technical_failure_if_max_retries_exceeded(sample_letter_notification, mocker):
    mocker.patch('app.celery.letters_pdf_tasks.notify_celery.send_task', side_effect=Exception())
    mocker.patch('app.celery.letters_pdf_tasks.sanitise_letter.retry', side_effect=MaxRetriesExceededError())

    filename = 'NOTIFY.{}'.format(sample_letter_notification.reference)
    sample_letter_notification.status = NOTIFICATION_PENDING_VIRUS_CHECK

    with pytest.raises(NotificationTechnicalFailureException):
        sanitise_letter(filename)

    assert sample_letter_notification.status == NOTIFICATION_TECHNICAL_FAILURE


@mock_s3
@pytest.mark.parametrize('key_type, destination_bucket, expected_status, destination_filename', [
    (KEY_TYPE_NORMAL, 'LETTERS_PDF_BUCKET_NAME', NOTIFICATION_CREATED, '2018-07-01/NOTIFY.foo'),
    (KEY_TYPE_TEST, 'TEST_LETTERS_BUCKET_NAME', NOTIFICATION_DELIVERED, 'NOTIFY.foo'),
])
def test_process_sanitised_letter_with_valid_letter(
    sample_letter_notification,
    key_type,
    destination_bucket,
    expected_status,
    destination_filename,
):
    filename = 'NOTIFY.{}'.format(sample_letter_notification.reference)

    scan_bucket_name = current_app.config['LETTERS_SCAN_BUCKET_NAME']
    template_preview_bucket_name = current_app.config['LETTER_SANITISE_BUCKET_NAME']
    destination_bucket_name = current_app.config[destination_bucket]
    conn = boto3.resource('s3', region_name='eu-west-1')

    scan_bucket = conn.create_bucket(Bucket=scan_bucket_name)
    template_preview_bucket = conn.create_bucket(Bucket=template_preview_bucket_name)
    destination_bucket = conn.create_bucket(Bucket=destination_bucket_name)

    s3 = boto3.client('s3', region_name='eu-west-1')
    s3.put_object(Bucket=scan_bucket_name, Key=filename, Body=b'original_pdf_content')
    s3.put_object(Bucket=template_preview_bucket_name, Key=filename, Body=b'sanitised_pdf_content')

    sample_letter_notification.status = NOTIFICATION_PENDING_VIRUS_CHECK
    sample_letter_notification.key_type = key_type
    sample_letter_notification.billable_units = 1
    sample_letter_notification.created_at = datetime(2018, 7, 1, 12)

    encrypted_data = encryption.encrypt({
        'page_count': 2,
        'message': None,
        'invalid_pages': None,
        'validation_status': 'passed',
        'filename': filename,
        'notification_id': str(sample_letter_notification.id),
        'address': 'A. User\nThe house on the corner'
    })
    process_sanitised_letter(encrypted_data)

    assert sample_letter_notification.status == expected_status
    assert sample_letter_notification.billable_units == 1
    assert sample_letter_notification.to == 'A. User\nThe house on the corner'

    assert not [x for x in scan_bucket.objects.all()]
    assert not [x for x in template_preview_bucket.objects.all()]
    assert len([x for x in destination_bucket.objects.all()]) == 1

    file_contents = conn.Object(destination_bucket_name, destination_filename).get()['Body'].read().decode('utf-8')
    assert file_contents == 'sanitised_pdf_content'


@mock_s3
@pytest.mark.parametrize('key_type', [KEY_TYPE_NORMAL, KEY_TYPE_TEST])
def test_process_sanitised_letter_with_invalid_letter(sample_letter_notification, key_type):
    filename = 'NOTIFY.{}'.format(sample_letter_notification.reference)

    scan_bucket_name = current_app.config['LETTERS_SCAN_BUCKET_NAME']
    template_preview_bucket_name = current_app.config['LETTER_SANITISE_BUCKET_NAME']
    invalid_letter_bucket_name = current_app.config['INVALID_PDF_BUCKET_NAME']
    conn = boto3.resource('s3', region_name='eu-west-1')

    scan_bucket = conn.create_bucket(Bucket=scan_bucket_name)
    template_preview_bucket = conn.create_bucket(Bucket=template_preview_bucket_name)
    invalid_letter_bucket = conn.create_bucket(Bucket=invalid_letter_bucket_name)

    s3 = boto3.client('s3', region_name='eu-west-1')
    s3.put_object(Bucket=scan_bucket_name, Key=filename, Body=b'original_pdf_content')

    sample_letter_notification.status = NOTIFICATION_PENDING_VIRUS_CHECK
    sample_letter_notification.key_type = key_type
    sample_letter_notification.billable_units = 1
    sample_letter_notification.created_at = datetime(2018, 7, 1, 12)

    encrypted_data = encryption.encrypt({
        'page_count': 2,
        'message': 'content-outside-printable-area',
        'invalid_pages': [1],
        'validation_status': 'failed',
        'filename': filename,
        'notification_id': str(sample_letter_notification.id),
        'address': None,
    })
    process_sanitised_letter(encrypted_data)

    assert sample_letter_notification.status == NOTIFICATION_VALIDATION_FAILED
    assert sample_letter_notification.billable_units == 0

    assert not [x for x in scan_bucket.objects.all()]
    assert not [x for x in template_preview_bucket.objects.all()]
    assert len([x for x in invalid_letter_bucket.objects.all()]) == 1

    file_contents = conn.Object(invalid_letter_bucket_name, filename).get()['Body'].read().decode('utf-8')
    assert file_contents == 'original_pdf_content'


def test_process_sanitised_letter_when_letter_status_is_not_pending_virus_scan(
    sample_letter_notification,
    mocker,
):
    mock_s3 = mocker.patch('app.celery.letters_pdf_tasks.s3')
    sample_letter_notification.status = NOTIFICATION_CREATED

    encrypted_data = encryption.encrypt({
        'page_count': 2,
        'message': None,
        'invalid_pages': None,
        'validation_status': 'passed',
        'filename': 'NOTIFY.{}'.format(sample_letter_notification.reference),
        'notification_id': str(sample_letter_notification.id),
        'address': None
    })
    process_sanitised_letter(encrypted_data)

    assert not mock_s3.called


def test_process_sanitised_letter_puts_letter_into_tech_failure_for_boto_errors(
    sample_letter_notification,
    mocker,
):
    mocker.patch('app.celery.letters_pdf_tasks.s3.get_s3_object', side_effect=ClientError({}, 'operation_name'))
    sample_letter_notification.status = NOTIFICATION_PENDING_VIRUS_CHECK

    encrypted_data = encryption.encrypt({
        'page_count': 2,
        'message': None,
        'invalid_pages': None,
        'validation_status': 'passed',
        'filename': 'NOTIFY.{}'.format(sample_letter_notification.reference),
        'notification_id': str(sample_letter_notification.id),
        'address': None
    })

    with pytest.raises(NotificationTechnicalFailureException):
        process_sanitised_letter(encrypted_data)

    assert sample_letter_notification.status == NOTIFICATION_TECHNICAL_FAILURE


def test_process_sanitised_letter_retries_if_there_is_an_exception(
    mocker,
    sample_letter_notification,
):
    mocker.patch('app.celery.letters_pdf_tasks.update_letter_pdf_status', side_effect=Exception())
    mock_celery_retry = mocker.patch('app.celery.letters_pdf_tasks.process_sanitised_letter.retry')

    sample_letter_notification.status = NOTIFICATION_PENDING_VIRUS_CHECK
    encrypted_data = encryption.encrypt({
        'page_count': 2,
        'message': None,
        'invalid_pages': None,
        'validation_status': 'passed',
        'filename': 'NOTIFY.{}'.format(sample_letter_notification.reference),
        'notification_id': str(sample_letter_notification.id),
        'address': None
    })

    process_sanitised_letter(encrypted_data)

    mock_celery_retry.assert_called_once_with(queue='retry-tasks')


def test_process_sanitised_letter_puts_letter_into_technical_failure_if_max_retries_exceeded(
    mocker,
    sample_letter_notification,
):
    mocker.patch('app.celery.letters_pdf_tasks.update_letter_pdf_status', side_effect=Exception())
    mocker.patch('app.celery.letters_pdf_tasks.process_sanitised_letter.retry', side_effect=MaxRetriesExceededError())

    sample_letter_notification.status = NOTIFICATION_PENDING_VIRUS_CHECK
    encrypted_data = encryption.encrypt({
        'page_count': 2,
        'message': None,
        'invalid_pages': None,
        'validation_status': 'passed',
        'filename': 'NOTIFY.{}'.format(sample_letter_notification.reference),
        'notification_id': str(sample_letter_notification.id),
        'address': None
    })

    with pytest.raises(NotificationTechnicalFailureException):
        process_sanitised_letter(encrypted_data)

    assert sample_letter_notification.status == NOTIFICATION_TECHNICAL_FAILURE


def test_process_letter_task_check_virus_scan_failed(sample_letter_notification, mocker):
    filename = 'NOTIFY.{}'.format(sample_letter_notification.reference)
    sample_letter_notification.status = NOTIFICATION_PENDING_VIRUS_CHECK
    mock_move_failed_pdf = mocker.patch('app.celery.letters_pdf_tasks.move_failed_pdf')

    with pytest.raises(VirusScanError) as e:
        process_virus_scan_failed(filename)

    assert "Virus scan failed:" in str(e.value)
    mock_move_failed_pdf.assert_called_once_with(filename, ScanErrorType.FAILURE)
    assert sample_letter_notification.status == NOTIFICATION_VIRUS_SCAN_FAILED


def test_process_letter_task_check_virus_scan_error(sample_letter_notification, mocker):
    filename = 'NOTIFY.{}'.format(sample_letter_notification.reference)
    sample_letter_notification.status = NOTIFICATION_PENDING_VIRUS_CHECK
    mock_move_failed_pdf = mocker.patch('app.celery.letters_pdf_tasks.move_failed_pdf')

    with pytest.raises(VirusScanError) as e:
        process_virus_scan_error(filename)

    assert "Virus scan error:" in str(e.value)
    mock_move_failed_pdf.assert_called_once_with(filename, ScanErrorType.ERROR)
    assert sample_letter_notification.status == NOTIFICATION_TECHNICAL_FAILURE


def test_replay_letters_in_error_for_all_letters_in_error_bucket(notify_api, mocker):
    mockObject = boto3.resource('s3').Object('ERROR', 'ERROR/file_name')
    mocker.patch("app.celery.letters_pdf_tasks.get_file_names_from_error_bucket", return_value=[mockObject])
    mock_move = mocker.patch("app.celery.letters_pdf_tasks.move_error_pdf_to_scan_bucket")
    mock_celery = mocker.patch("app.celery.letters_pdf_tasks.notify_celery.send_task")
    replay_letters_in_error()
    mock_move.assert_called_once_with('file_name')
    mock_celery.assert_called_once_with(name='scan-file', kwargs={'filename': 'file_name'}, queue='antivirus-tasks')


def test_replay_letters_in_error_for_one_file(notify_api, mocker):
    mockObject = boto3.resource('s3').Object('ERROR', 'ERROR/file_name')
    mocker.patch("app.celery.letters_pdf_tasks.get_file_names_from_error_bucket", return_value=[mockObject])
    mock_move = mocker.patch("app.celery.letters_pdf_tasks.move_error_pdf_to_scan_bucket")
    mock_celery = mocker.patch("app.celery.letters_pdf_tasks.notify_celery.send_task")
    replay_letters_in_error("file_name")
    mock_move.assert_called_once_with('file_name')
    mock_celery.assert_called_once_with(name='scan-file', kwargs={'filename': 'file_name'}, queue='antivirus-tasks')
