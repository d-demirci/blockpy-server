import os
import time

import base64

from flask import url_for
from sqlalchemy import Column, Text, Integer, Boolean, ForeignKey, Index, func, String
from sqlalchemy.orm import relationship

from main import app
from models import models
from models.models import Base, db, ensure_dirs, optional_encoded_field, datetime_to_string


class SubmissionStatuses:
    # Started -> not yet run
    STARTED = "Started"
    # inProgress -> Run, but not yet marked formally as "submitted"
    IN_PROGRESS = "inProgress"
    # Submitted -> formally marked
    SUBMITTED = "Submitted"
    # Completed -> Either formally Submitted and FullyGraded, or auto graded as "correct"
    COMPLETED = "Completed"


class GradingStatuses:
    FULLY_GRADED = 'FullyGraded'
    PENDING = 'Pending'
    PENDING_MANUAL = 'PendingManual'
    FAILED = 'Failed'
    NOT_READY = 'NotReady'


class Submission(Base):
    code = Column(Text(), default="")
    extra_files = Column(Text(), default="")
    url = Column(Text(), default="")
    endpoint = Column(Text(), default="")
    # Should be treated as out of X/100
    score = Column(Integer(), default=0)
    correct = Column(Boolean(), default=False)
    submission_status = Column(String(255), default=SubmissionStatuses.STARTED)
    grading_status = Column(String(255), default=GradingStatuses.NOT_READY)
    # Tracking
    assignment_id = Column(Integer(), ForeignKey('assignment.id'))
    assignment_group_id = Column(Integer(), ForeignKey('assignment_group.id'), nullable=True)
    course_id = Column(Integer(), ForeignKey('course.id'))
    user_id = Column(Integer(), ForeignKey('user.id'))
    assignment_version = Column(Integer(), default=0)
    version = Column(Integer(), default=0)

    assignment = relationship("Assignment")

    __table_args__ = (Index('submission_index', "assignment_id",
                            "course_id", "user_id"),)

    def encode_json(self, use_owner=True):
        return {
            '_schema_version': 2,
            'code': self.code,
            'extra_files': self.extra_files,
            'url': self.url,
            'endpoint': self.endpoint,
            'score': self.score,
            'correct': self.correct,
            'assignment_id': self.assignment_id,
            'course_id': self.course_id,
            'user_id': self.user_id,
            'assignment_version': self.assignment_version,
            'version': self.version,
            'submission_status': self.submission_status,
            'grading_status': self.grading_status,
            'user_id__email': optional_encoded_field(self.user_id, use_owner, models.User.query.get, 'email'),
            'id': self.id,
            'date_modified': datetime_to_string(self.date_modified),
            'date_created': datetime_to_string(self.date_created)
        }

    @staticmethod
    def by_id(submission_id):
        return Submission.query.get(submission_id)

    @staticmethod
    def full_by_id(submission_id):
        result = (db.session.query(Submission, models.User, models.Assignment)
                  .filter(Submission.user_id == models.User.id)
                  .filter(Submission.assignment_id == models.Assignment.id)
                  .filter(Submission.id == submission_id)
                  .first())
        if result is None:
            return None, None, None
        else:
            return result

    @staticmethod
    def by_assignment(assignment_id, course_id):
        return (db.session.query(Submission, models.User, models.Assignment)
                .filter(Submission.user_id == models.User.id)
                .filter(Submission.assignment_id == models.Assignment.id)
                .filter(Submission.assignment_id == assignment_id)
                .filter(Submission.course_id == course_id)
                .all())

    @staticmethod
    def get_latest(assignment_id, course_id):
        return (db.session.query(func.max(Submission.date_modified))
                .filter(Submission.course_id == course_id,
                        Submission.assignment_id == assignment_id)
                .group_by(Submission.user_id)
                .order_by(func.max(Submission.date_modified).desc())
                .count())

    @staticmethod
    def by_student(user_id, course_id):
        return (db.session.query(Submission, models.User, models.Assignment)
                .filter(Submission.user_id == models.User.id)
                .filter(Submission.assignment_id == models.Assignment.id)
                .filter(Submission.user_id == user_id)
                .filter(Submission.course_id == course_id)
                .all())

    def __str__(self):
        return '<Submission {} for {}>'.format(self.id, self.user_id)

    def full_status(self):
        if self.assignment.hidden:
            return "????"
        elif self.correct:
            return "Complete"
        elif self.assignment.reviewed:
            if self.grading_status == "PendingManual":
                return "Pending review"
        elif self.score:
            return "Incomplete ({}%)".format(self.status)
        else:
            return "Incomplete"

    def full_score(self):
        return float(self.correct) or self.score / 100.0

    @staticmethod
    def from_assignment(assignment, user_id, course_id, assignment_group_id=None):
        submission = Submission(assignment_id=assignment.id,
                                user_id=user_id,
                                assignment_group_id=assignment_group_id,
                                course_id=course_id,
                                code=assignment.starting_code,
                                extra_files=assignment.extra_starting_files,
                                assignment_version=assignment.version)
        db.session.add(submission)
        db.session.commit()
        return submission

    @staticmethod
    def get_submission(assignment_id, user_id, course_id):
        return Submission.query.filter_by(assignment_id=assignment_id,
                                          course_id=course_id,
                                          user_id=user_id).first()

    @staticmethod
    def load_or_new(assignment, user_id, course_id, new_submission_url="", assignment_group_id=None):
        submission = Submission.get_submission(assignment.id, user_id, course_id)
        if not submission:
            submission = Submission.from_assignment(assignment, user_id, course_id, assignment_group_id)

        if new_submission_url:
            submission.endpoint = new_submission_url
            db.session.commit()
        return submission

    STUDENT_FILENAMES = ("#extra_student_files.blockpy", "answer.py")

    def save_code(self, filename, code):
        if filename == "#extra_student_files.blockpy":
            self.extra_files = code
        elif filename == "answer.py":
            self.code = code
        self.version += 1
        self.assignment_version = self.assignment.version
        db.session.commit()

    def update_submission(self, score, correct):
        was_changed = self.score != score or self.correct != correct
        self.score = score
        self.correct = correct
        if self.correct:
            self.submission_status = SubmissionStatuses.COMPLETED
            self.grading_status = GradingStatuses.FULLY_GRADED
        db.session.commit()
        return was_changed

    @staticmethod
    def save_correct(user_id, assignment_id, course_id):
        submission = Submission.query.filter_by(user_id=user_id,
                                                assignment_id=assignment_id,
                                                course_id=course_id).first()
        if not submission:
            submission = Submission(assignment_id=assignment_id,
                                    user_id=user_id,
                                    course_id=course_id,
                                    correct=True)
            db.session.add(submission)
        else:
            submission.correct = True
        db.session.commit()
        return submission

    def set_status(self, new_value):
        was_changed = self.status != new_value
        self.status = new_value
        db.session.commit()
        return was_changed

    def get_report_blockpy(self, image=""):
        if self.correct:
            message = "Success!"
        else:
            message = "Incomplete"

    def get_block_image(self):
        sub_blocks_folder = os.path.join(app.config['UPLOADS_DIR'], 'submission_blocks')
        image_path = os.path.join(sub_blocks_folder, str(self.id) + '.png')
        if os.path.exists(image_path):
            return url_for('blockpy.get_submission_image',
                           submission_id=self.id,
                           _external=True)
        return ""

    def save_block_image(self, image=""):
        sub_blocks_folder = os.path.join(app.config['UPLOADS_DIR'], 'submission_blocks')
        image_path = os.path.join(sub_blocks_folder, str(self.id) + '.png')
        if image != "":
            converted_image = base64.b64decode(image[22:])
            with open(image_path, 'wb') as image_file:
                image_file.write(converted_image)
            return url_for('blockpy.get_submission_image',
                           submission_id=self.id,
                           _external=True)
        elif os.path.exists(image_path):
            try:
                os.remove(image_path)
            except Exception as e:
                app.logger.info("Could not delete because:" + str(e))
        return None

    def log_code(self, course_id, extension='.py', timestamp=''):
        '''
        Store the code on disk, mapped to the Assignment ID and the Student ID
        '''
        # Multiple-file logging
        log = models.Log.new('code', 'set', self.assignment_id, self.assignment_version,
                             course_id,
                             self.user_id, body=self.code, timestamp=timestamp)

        directory = os.path.join(app.config['BLOCKPY_LOG_DIR'],
                                 str(self.assignment_id),
                                 str(self.user_id))

        ensure_dirs(directory)
        name = time.strftime("%Y%m%d-%H%M%S")
        file_name = os.path.join(directory, name + extension)

        with open(file_name, 'w') as blockly_logfile:
            blockly_logfile.write(self.code)
