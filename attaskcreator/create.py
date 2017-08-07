"""Main functions for attaskcreator."""
# import json
import datetime
import re
import logging
import daiquiri
from smtplib import SMTP
from attaskcreator import settings
from attaskcreator.config import get_settings
from attaskcreator import retrievemail
from attaskcreator import s3interface
from attaskcreator import exceptions

LOGFILE = "/tmp/attest.log"


def choose_phrase(phrases, text):
    """Chooses the first phrase from a list of phrases that is found in the
    given text."""
    for phrase in phrases:
        if phrase.lower() in text.lower():
            return phrase
    # if a trigger phrase was not found, raise an exception.
    raise exceptions.NoPhraseError("Phrase was not found in {}.".format(text))


def parse_email_message(params, text_to_search):
    """Returns text found between a prefix and terminating character or None
    if it is not found.

    params: tuple of prefix phrase and terminating character.
    text_to_search: self-explanatory.
    """
    phrases, term_char = params
    # will raise exception if nothing found, propogates to main.
    trigger_phrase = choose_phrase(phrases, text_to_search)
    # clean text_to_search
    text_oneline = text_to_search.replace('\n', ' ')
    text_clean = ' '.join(text_oneline.split())
    regex = re.compile(r'({} )([^{}]*)'.format(trigger_phrase, term_char),
                       re.IGNORECASE
                       )
    found_text = regex.search(text_clean)
    try:
        return found_text.group(2)
    except AttributeError:
        raise exceptions.RegexFailedError(('Could not find text after {tp} in'
                                           ' after {st}').format(
                                               tp=trigger_phrase,
                                               st=text_clean
                                           ))


def main():
    """Main function for attaskcreator."""
    get_settings()
    daiquiri.setup(level=logging.INFO, outputs=(
        daiquiri.output.File(LOGFILE),)
    )
    logger = daiquiri.getLogger(__name__)
    mail = retrievemail.FetchMail(
        settings.eml_imap_server
        )
    messages = mail.fetch_unread_messages(settings.eml_username,
                                          settings.eml_pwd)
    atdb = settings.database

    # loop through email messages
    for mess in messages:
        data = retrievemail.read_msg_info(mess)

        try:
            parsed_text = parse_email_message(
                (settings.trigger_phrases, settings.term_char), data['body']
                )
            # get needed info
            to_info = retrievemail.parse_to_field(data['to'])
            people = []
            for person in to_info:
                person_id = atdb.search_for_email(
                    settings.at_people_table,
                    (settings.people_table_key, person['email']),
                    ("First Name", person['fname']),
                    ("Last Name", person['lname'])
                    )
                people.append(person_id)

            # save any attachments
            attachments = retrievemail.save_attachments(mess,
                                                        "/tmp/attach_down")

            # if there are attachments, upload them to s3 and send urls to
            # airtable.
            file_info = ()
            if attachments:
                try:
                    s3_urls = []
                    for path in attachments:
                        url = s3interface.make_url(path, settings.bucket)
                        s3_urls.append(url)
                    # tag text with date to prevent duplicate records
                    tagged_text = ' T:'.join(
                        (parsed_text, str(datetime.datetime.today()))
                        )
                    file_rec = atdb.upload_attach(
                        settings.at_files_table,
                        (settings.files_table_name_field, tagged_text),
                        (settings.files_table_attach_field, s3_urls)
                        )
                    file_info = (settings.tasks_table_attach, file_rec)
                except exceptions.NoAttachmentError:
                    file_info = ()

            notes_info = ()
            if settings.tasks_table_notes is not None:
                notes_info = (settings.tasks_table_notes, data['body'])

            # pass to record method for creation
            atdb.create_task_record(
                settings.at_tasks_table,
                (settings.tasks_table_text, parsed_text),
                (settings.tasks_table_person, people),
                notes_info,
                file_info
                )
        except (exceptions.RegexFailedError, exceptions.NoPhraseError):
            logger.exception("No record was created.")
            subject = 'Failed to create airtable task record'
            body = ('The trigger phrase was not found in your email to '
                    + data['to']
                    + ', so a record was not created. The body of the email'
                    + 'is below:\n\n'
                    + 'Subject: {}\n'.format(data['subject'])
                    + data['body']
                    )
            retrievemail.sendmsg(
                SMTP(settings.eml_smtp_server, 587),
                (settings.eml_username, settings.eml_pwd),
                ('Airtable Task Creator', settings.eml_username),
                ('User', settings.eml_error),
                (subject, body)
                )


if __name__ == "__main__":
    main()
