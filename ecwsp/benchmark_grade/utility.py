#   Copyright 2012 Burke Software and Consulting LLC
#   Author: John Milner <john@tmoj.net>
#   
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation; either version 2 of the License, or
#   (at your option) any later version.
#     
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#      
#   You should have received a copy of the GNU General Public License
#   along with this program; if not, write to the Free Software
#   Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#   MA 02110-1301, USA.

from ecwsp.benchmark_grade.models import CalculationRule, Aggregate, Item, Mark, Category
from ecwsp.schedule.models import MarkingPeriod
from ecwsp.grades.models import Grade
from django.db.models import Avg, Sum, Min, Max
import logging
from decimal import Decimal, ROUND_HALF_UP

def benchmark_find_calculation_rule(school_year):
    rules = CalculationRule.objects.filter(first_year_effective=school_year)
    if rules.count():
        # We have a rule explicitly matching this marking period's school year
        rule = rules[0]
    else:
        # No explicit match, so find the most recent rule that went into effect *before* this marking period's school year
        rules = CalculationRule.objects.filter(first_year_effective__start_date__lt=school_year.start_date).order_by('-first_year_effective__start_date')
        if rules.count():
            rule = rules[0]
        else:
            raise Exception('There is no suitable calculation rule for the school year {}.'.format(school_year))
    return rule

def benchmark_get_create_or_flush(model_base, **kwargs):
    # make sure there is one and only one object matching our criteria
    try:
        model, created = model_base.objects.get_or_create(**kwargs)
    except model_base.MultipleObjectsReturned:
        # unsure why this happens, but it does
        bad = model_base.objects.filter(**kwargs)
        logging.error('Expected 0 or 1 {} but found {}; flushing them all!'.format(str(model_base).split("'")[1], bad.count()), exc_info=True)
        bad.delete()
        model, created = model_base.objects.get_or_create(**kwargs)
    return model, created

def benchmark_get_or_flush(model_base, **kwargs):
    # make sure there is at most one object matching our criteria
    # if there are two or more, don't try to guess at the correct one; just delete them
    try:
        model = model_base.objects.get(**kwargs)
    except model_base.MultipleObjectsReturned:
        # unsure why this happens, but it does
        bad = model_base.objects.filter(**kwargs)
        logging.error('Expected 1 {} but found {}; flushing them all!'.format(str(model_base).split("'")[1], bad.count()), exc_info=True)
        bad.delete()
        raise model_base.DoesNotExist
    return model

def benchmark_calculate_category_as_course_aggregate(student, category, marking_period):
    agg, created = benchmark_get_create_or_flush(Aggregate, student=student, course=None, category=category, marking_period=marking_period)
    agg.name = 'G! {} - {} (All Courses, {})'.format(student, category, marking_period)
    agg.cached_substitution = None
    calculation_rule = benchmark_find_calculation_rule(marking_period.school_year)
    category_as_course = calculation_rule.category_as_course_set.get(category=category)
    category_numer = category_denom = Decimal(0)
    for course in Course.objects.filter(courseenrollment__user__username=student.username, marking_period=marking_period, department__in=category_as_course.include_departments.all()).distinct():
        credits = Decimal(course.credits) / course.marking_period.count()
        try:
            category_aggregate = Aggregate.objects.get(student=student, marking_period=marking_period, category=category, course=course)
        except Aggregate.DoesNotExist:
            category_aggregate = benchmark_calculate_course_category_aggregate(student, course, category, marking_period)[0]
        if category_aggregate is not None and category_aggregate.cached_value is not None:
            calculate_as, display_as = calculation_rule.substitute(category_aggregate, category_aggregate.cached_value)
            category_numer += credits * calculate_as
            category_denom += credits
            # yes, agg will just end up with the last substitution, but tough
            if display_as is not None:
                agg.cached_substitution = display_as
    if category_denom:
        agg.cached_value = category_numer / category_denom
    else:
        agg.cached_value = None
    agg.save()
    return agg, created

def benchmark_calculate_course_category_aggregate(student, course, category, marking_period, items=None):
    if items is None:
        items = Item.objects.all()
        save = True
    else:
        # don't store aggregates for every one-off combination of items
        save = False
    items = items.filter(course=course, category=category)
    # if we're passed marking_period=None, we should consider items across the entire duration of the course
    # if we're passed a specific marking period instead, we should consider items matching only that marking period
    if marking_period is not None:
        items = items.filter(marking_period=marking_period)

    calculation_rule = benchmark_find_calculation_rule(course.marking_period.all()[0].school_year)

    # initialize attributes
    criteria = {'student': student, 'course': course, 'category': category, 'marking_period': marking_period}
    # silly name is silly, and should not be part of the criteria
    silly_name = 'G! {} - {} ({}, {})'.format(student, category, course, marking_period)
    # don't use get_or_create; otherwise we may end up saving an empty object
    try:
        agg = benchmark_get_or_flush(Aggregate, **criteria)
        created = False
    except Aggregate.DoesNotExist:
        agg = Aggregate(**criteria)
        created = True
    agg.name = silly_name

    # begin the actual calculations!
    agg.cached_substitution = None
    category_numer = category_denom = Decimal(0)
    if category.allow_multiple_demonstrations:
        for category_item in items.exclude(points_possible=None):
            # Find the highest mark amongst demonstrations and count it as the grade for the item
            best = Mark.objects.filter(student=student, item=category_item).aggregate(Max('mark'))['mark__max']
            if best is not None:
                calculate_as, display_as = calculation_rule.substitute(category_item, best)
                category_numer += calculate_as
                category_denom += category_item.points_possible
                # yes, agg will just end up with the last substitution, but tough
                if display_as is not None:
                    agg.cached_substitution = display_as

    else:
        for category_mark in Mark.objects.filter(student=student, item__in=items).exclude(mark=None).exclude(item__points_possible=None):
            calculate_as, display_as = calculation_rule.substitute(category_mark.item, category_mark.mark)
            category_numer += calculate_as
            category_denom += category_mark.item.points_possible
            if display_as is not None:
                agg.cached_substitution = display_as
    if category_denom:
        agg.cached_value = category_numer / category_denom * 4 # TODO: don't hard code this
    else:
        agg.cached_value = None
    if save:
        agg.save()
    return agg, created

def benchmark_calculate_course_aggregate(student, course, marking_period, items=None, recalculate_all_categories=False):
    # doesn't recalculate component aggregates by default
    if items is None:
        # just leave items alone--we don't actually consider it here; we only pass it to benchmark_calculate_course_category_aggregate
        # setting items here will prevent benchmark_calculate_course_category_aggregate from saving anything
        save = True
        items_categories = ()
    else:
        # don't store aggregates for every one-off combination of items
        save = False
        # we'll have to miss cache and recaculate any category to which an item belongs
        items_categories = Category.objects.filter(item__in=items).distinct()

    calculation_rule = benchmark_find_calculation_rule(course.marking_period.all()[0].school_year)

    # initialize attributes
    criteria = {'student': student, 'course': course, 'category': None, 'marking_period': marking_period}
    # silly name is silly, and should not be part of the criteria
    silly_name = 'G! {} - Course Average ({}, {})'.format(student, course, marking_period)
    # don't use get_or_create; otherwise we may end up saving an empty object
    try:
        agg = benchmark_get_or_flush(Aggregate, **criteria)
        created = False
    except Aggregate.DoesNotExist:
        agg = Aggregate(**criteria)
        created = True
    agg.name = silly_name

    # begin the actual calculations!
    agg.cached_substitution = None
    course_numer = course_denom = Decimal(0)
    for rule_category in calculation_rule.per_course_category_set.filter(apply_to_departments=course.department):
        criteria['category'] = rule_category.category
        cat_agg, cat_created = benchmark_get_create_or_flush(Aggregate, **criteria)
        if cat_created or recalculate_all_categories or rule_category.category in items_categories:
            cat_agg, cat_created = benchmark_calculate_course_category_aggregate(student, course, rule_category.category, marking_period, items)
        if cat_agg.cached_value is not None:
            course_numer += rule_category.weight * cat_agg.cached_value
            course_denom += rule_category.weight
            # yes, agg will just end up with the last substitution, but tough
            if cat_agg.cached_substitution is not None:
                agg.cached_substitution = cat_agg.cached_substitution
    if course_denom:
        agg.cached_value = course_numer / course_denom
    else:
        agg.cached_value = None
    if save:
        agg.save()
        if marking_period is not None:
            # temporary(?) integration with the rest of sword
            g, g_created = Grade.objects.get_or_create(student=student, course=course, marking_period=marking_period, override_final=False)
            if agg.cached_substitution is not None:
                # FIDDLESTICKS... INC does not fit in the column
                letter_grade_max_length = Grade._meta.get_field_by_name('letter_grade')[0].max_length
                g.letter_grade = agg.cached_substitution[:letter_grade_max_length]
                g.grade = None
            else:
                g.set_grade(agg.cached_value)
            g.save()
    return agg, created

def gradebook_recalculate_on_item_change(item, students=None):
    if students is None:
        students = item.course.get_enrolled_students()
    course = item.course
    category = item.category
    marking_period = item.marking_period
    calculation_rule = benchmark_find_calculation_rule(item.course.marking_period.all()[0].school_year)
    affects_overall_course = calculation_rule.per_course_category_set.filter(category=category, apply_to_departments=course.department).count() > 0
    affects_overall_category = calculation_rule.category_as_course_set.filter(category=category, include_departments=course.department).count() > 0

    for student in students:
        # always recalculate the aggregate for this course, category, and marking period
        benchmark_calculate_course_category_aggregate(student, course, category, marking_period)
        if affects_overall_course:
            benchmark_calculate_course_aggregate(student, course, marking_period) 
        if affects_overall_category:
            benchmark_calculate_category_as_course_aggregate(student, category, marking_period)
        # always recalculate the course-long (i.e. marking_period=None) aggregate for this category
        benchmark_calculate_course_category_aggregate(student, course, category, None)
        if affects_overall_course:
            benchmark_calculate_course_aggregate(student, course, None) 

def gradebook_recalculate_on_mark_change(mark):
    gradebook_recalculate_on_item_change(mark.item, (mark.student, ))

def gradebook_get_average(student, course, category=None, marking_period=None, items=None):
    try:
        if items is not None: # averages of one-off sets of items aren't saved and must be calculated every time
            # this is rather silly, but it avoids code duplication or a teensy four-line function.
            raise Aggregate.DoesNotExist
        agg = benchmark_get_or_flush(Aggregate, student=student, course=course, category=category, marking_period=marking_period)
    except Aggregate.DoesNotExist:
        if category is None:
            agg, created = benchmark_calculate_course_aggregate(student, course, marking_period, items)
        else:
            agg, created = benchmark_calculate_course_category_aggregate(student, course, category, marking_period, items)
    if agg.cached_substitution is not None:
        return agg.cached_substitution
    elif agg.cached_value is not None:
        calculation_rule = benchmark_find_calculation_rule(course.marking_period.all()[0].school_year)
        if category is not None and category.display_scale is not None:
            pretty = agg.cached_value / 4 * category.display_scale # TODO: use agg.points_possible (and actually set it when aggregates are calculated)
            pretty = '{}{}'.format(pretty.quantize(Decimal(10) ** (-1 * calculation_rule.decimal_places), ROUND_HALF_UP), category.display_symbol)
        else:
            pretty = agg.cached_value.quantize(Decimal(10) ** (-1 * calculation_rule.decimal_places), ROUND_HALF_UP)
        return pretty
    else:
        return None

def gradebook_get_category_average(student, category, marking_period):
    try:
        agg = benchmark_get_or_flush(Aggregate, student=student, course=None, category=category, marking_period=marking_period)
    except Aggregate.DoesNotExist:
        agg, created = benchmark_calculate_category_as_course_aggregate(student, category, marking_period)
    if agg.cached_substitution is not None:
        return agg.cached_substitution
    elif agg.cached_value is not None:
        calculation_rule = benchmark_find_calculation_rule(marking_period.school_year)
        if category.display_scale is not None:
            pretty = agg.cached_value / 4 * category.display_scale # TODO: use agg.points_possible (and actually set it when aggregates are calculated)
            pretty = '{}{}'.format(pretty.quantize(Decimal(10) ** (-1 * calculation_rule.decimal_places), ROUND_HALF_UP), category.display_symbol)
        else:
            pretty = agg.cached_value.quantize(Decimal(10) ** (-1 * calculation_rule.decimal_places), ROUND_HALF_UP)
        return pretty
    else:
        return None


''' ye olde belowe '''

def benchmark_ruled_calculate_grade_for_courses(student, courses, marking_period=None, date_report=None):
    # TODO: Decimal places configuration value
    DECIMAL_PLACES = 2
    # student: a single student
    # courses: all courses involved in the GPA calculation
    # marking_period: restricts GPA calculation to a _single_ marking period
    # date_report: restricts GPA calculation to marking periods _ending_ on or before a date

    mps = None
    if marking_period is not None:
        mps = MarkingPeriod.objects.filter(id=(marking_period.id))
    else:
        mps = MarkingPeriod.objects.filter(id__in=courses.values('marking_period').distinct())
        if date_report is not None:
            mps = mps.filter(end_date__lte=date_report)
        else:
            mps = course.marking_period.all()

    student_numer = student_denom = float(0)
    for mp in mps.filter(school_year__benchmark_grade=True):
        mp_numer = mp_denom = float(0)
        rule = benchmark_find_calculation_rule(mp.school_year)
        for course in courses.filter(marking_period=mp).exclude(credits=None).distinct(): # IMO, Course.credits should be required, and we should not treat None as 0.
            # Handle per-course categories according to the calculation rule
            course_numer = course_denom = float(0)
            for category in rule.per_course_category_set.filter(apply_to_departments=course.department):
                try: category_aggregate = Aggregate.objects.get(student=student, marking_period=mp, course=course, category=category.category)
                except Aggregate.DoesNotExist: category_aggregate = None
                if category_aggregate is not None and category_aggregate.cached_value is not None:
                    # simplified normalization; assumes minimum is 0
                    normalized_value = category_aggregate.cached_value / rule.points_possible
                    course_numer += float(category.weight) * float(normalized_value)
                    course_denom += float(category.weight)
            if course_denom > 0:
                credits = float(course.credits) / course.marking_period.count()
                mp_numer += credits * course_numer / course_denom
                mp_denom += credits

        # Handle aggregates of categories that are counted as courses
        # TODO: Change CalculationRule model to have a field for the weight of each category. For now, assume 1.
        # Categories as courses shouldn't increase the weight of a marking period!
        mp_denom_before_categories = mp_denom
        for category in rule.category_as_course_set.all():
            category_numer = category_denom = float(0)
            for course in courses.filter(marking_period=mp, department__in=category.include_departments.all()).distinct():
                credits = float(course.credits) / course.marking_period.count()
                try: category_aggregate = Aggregate.objects.get(student=student, marking_period=mp, category=category.category, course=course)
                except Aggregate.DoesNotExist: category_aggregate = None
                if category_aggregate is not None and category_aggregate.cached_value is not None:
                    # simplified normalization; assumes minimum is 0
                    normalized_value  = category_aggregate.cached_value / rule.points_possible
                    category_numer += credits * float(normalized_value)
                    category_denom += credits
            if category_denom > 0:
                mp_numer += category_numer / category_denom
                mp_denom += 1

        if mp_denom > 0:
            mp_numer *= 4 # HARD CODED 4.0 SCALE!!!
            student_numer += mp_numer / mp_denom * mp_denom_before_categories 
            student_denom += mp_denom_before_categories
            mp_denom = mp_denom_before_categories # in this version, mp_denom isn't used again, but this may save someone pain in the future.

    # Handle non-benchmark-grade years. Calculation rules don't apply.
    legacy_courses = courses.filter(marking_period__in=mps.filter(school_year__benchmark_grade=False))
    for course in legacy_courses.exclude(credits=None).distinct(): # IMO, Course.credits should be required, and we should not treat None as 0.
        try:
            grade, credits = student._calculate_grade_for_single_course(course, marking_period, date_report)
            student_numer += grade * credits
            student_denom += credits
        except:
            logging.warning('Legacy course grade calculation failed for student {}, course {}, marking_period {}, date_report {}'.format(student, course, marking_period, date_report), exc_info=True)
            
    if student_denom > 0:
        return Decimal(student_numer / student_denom).quantize(Decimal(10) ** (-1 * DECIMAL_PLACES), ROUND_HALF_UP)
    else:
        return 'N/A'

def benchmark_calculate_grade_for_courses(student, courses, marking_period=None, date_report=None):
    #print "b_c_g_f_c(", student, courses, marking_period, date_report, ")"
    return benchmark_ruled_calculate_grade_for_courses(student, courses, marking_period, date_report)

    ''' HEY, THE REST OF THIS FUNCTION IS OBSOLETE AND WILL BE TRASHED SOON!!! '''

    # trying to rewrite this for the fourth time.
    # student: guess what? a student!
    # courses: all courses involved in the GPA calculation
    # marking_period: restricts GPA calculation to a _single_ marking period
    # date_report: restricts GPA calculation to marking periods _ending_ on or before a date

    benchmark_individual_cat_name = ('Standards', )
    benchmark_aggregate_cat_name = ('Engagement', 'Organization')

    legacy_numerator = 0
    legacy_denominator = 0

    benchmark_individual_cat = []
    benchmark_aggregate_cat = []
    # dicts within dicts: {MarkingPeriod: {Category: float}}
    benchmark_individual_numer = {} # still come from Aggregate, but then go into GPA individually
    benchmark_individual_denom = {}
    benchmark_aggregate_numer = {} # get averaged and then glommed onto the GPA like extra courses
    benchmark_aggregate_denom = {}
    benchmark_mp_weight = {} # beef up benchmark_grade averages at the end to compete with legacy ones 
    for cat_name in benchmark_individual_cat_name:
        benchmark_individual_cat.append(Category.objects.get(name=cat_name))
    for cat_name in benchmark_aggregate_cat_name:
        benchmark_aggregate_cat.append(Category.objects.get(name=cat_name))

    for course in courses.distinct():
        #@!print 'Course', course
        if course.marking_period.order_by('-start_date')[0].school_year.benchmark_grade:
            # a benchmark_grade course!
            #@!print '\tIs a benchmark_grade course'
            mps = None
            if marking_period is not None:
                mps = (marking_period, )
            elif date_report is not None:
                mps = course.marking_period.filter(end_date__lte=date_report)
            else:
                mps = course.marking_period.all()
            for mp in mps:
                #@!print '\tMP', mp
                try: weight = float(course.credits) / course.marking_period.count()
                except TypeError: weight = 0
                benchmark_mp_weight[mp.id] = benchmark_mp_weight.get(mp.id, 0) + weight
                #@!print '\t\tWeight:', benchmark_mp_weight[mp.id], '(added', weight, '=', float(course.credits), '/', course.marking_period.count(), ')'
                for cat in benchmark_individual_cat:
                    #@!print '\t\tIndividual Category:', cat
                    try: agg = Aggregate.objects.get(student=student, course=course, category=cat, marking_period=mp)
                    except: continue
                    if agg.cached_value is None: continue
                    # awfulness to avoid throwing KeyErrors
                    mp_numer_dict = benchmark_individual_numer.get(mp.id, {})
                    mp_denom_dict = benchmark_individual_denom.get(mp.id, {})
                    mp_numer_dict[cat.id] = mp_numer_dict.get(cat.id, 0) + weight * float(agg.cached_value)
                    mp_denom_dict[cat.id] = mp_denom_dict.get(cat.id, 0) + weight
                    #@!print '\t\t\tNumerator:', mp_numer_dict[cat.id], '(added', weight * float(agg.cached_value), '=', weight, '*', float(agg.cached_value), ')'
                    #@!print '\t\t\tDenominator:', mp_denom_dict[cat.id], '(added', weight, ')'
                    benchmark_individual_numer[mp.id] = mp_numer_dict
                    benchmark_individual_denom[mp.id] = mp_denom_dict
                for cat in benchmark_aggregate_cat:
                    #@!print '\t\tAggregate Category:', cat
                    try: agg = Aggregate.objects.get(student=student, course=course, category=cat, marking_period=mp)
                    except: continue
                    if agg.cached_value is None: continue
                    mp_numer_dict = benchmark_aggregate_numer.get(mp.id, {})
                    mp_denom_dict = benchmark_aggregate_denom.get(mp.id, {})
                    mp_numer_dict[cat.id] = mp_numer_dict.get(cat.id, 0) + weight * float(agg.cached_value)
                    mp_denom_dict[cat.id] = mp_denom_dict.get(cat.id, 0) + weight
                    #@!print '\t\t\tNumerator:', mp_numer_dict[cat.id], '(added', weight * float(agg.cached_value), '=', weight, '*', float(agg.cached_value), ')'
                    #@!print '\t\t\tDenominator:', mp_denom_dict[cat.id], '(added', weight, ')'
                    benchmark_aggregate_numer[mp.id] = mp_numer_dict
                    benchmark_aggregate_denom[mp.id] = mp_denom_dict
        else:
            # legacy calculation
            #print '\tIs a legacy course'
            try:
                grade, credit = student._calculate_grade_for_single_course(course, marking_period, date_report)
                legacy_numerator += float(grade) * credit
                legacy_denominator += credit
                #print '"{}" {} {} {}'.format(course, grade, credit, float(grade) * credit)
                #print '\t\tNumerator:', legacy_numerator, '(added', float(grade) * credit, '=', credit, '*', float(grade), ')'
                #print '\t\tDenominator:', legacy_denominator, '(added', credit, ')'
            except Exception as e:
                #@!print 'Legacy course exception:', e 
                import traceback
                #@!print traceback.format_exc()
                pass

    #@!print 'Starting final calculation loop...'
    gpa_numerator = 0
    gpa_denominator = 0
    for (mp_id, mp_individual_numer) in benchmark_individual_numer.items():
        #@!print '\tMP:', mp_id, '(', MarkingPeriod.objects.get(id=mp_id), ')'
        numerator = 0
        denominator = 0
        for (cat_id, cat_agg) in mp_individual_numer.items():
            numerator += cat_agg
            denominator += benchmark_individual_denom[mp_id][cat_id]
        #@!print "\t\tafter individuals, gpa is", numerator / denominator, "(", numerator, "/", denominator, ")"
        # just because individuals exist for this mp, it doesn't mean that aggregates do too
        # so don't use [] syntax, use .get(mp_id, {})
        for (cat_id, cat_agg) in benchmark_aggregate_numer.get(mp_id, {}).items():
            numerator += cat_agg / benchmark_aggregate_denom[mp_id][cat_id]
            #@!print "\t\tglomming on", Category.objects.get(id=cat_id), cat_agg / benchmark_aggregate_denom[mp_id][cat_id]
            denominator += 1
        #print numerator, denominator
        if denominator > 0:
            gpa_numerator += (numerator / denominator) * benchmark_mp_weight[mp_id]
            gpa_denominator += benchmark_mp_weight[mp_id]
    #print benchmark_mp_weight
    #print "\tnew", gpa_numerator, gpa_denominator
    #print "\tlegacy", legacy_numerator, legacy_denominator
    gpa_numerator += legacy_numerator
    gpa_denominator += legacy_denominator
    if gpa_denominator > 0:
        return Decimal(str(gpa_numerator / gpa_denominator)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    else:
        return "N/A" # follow the modelo
